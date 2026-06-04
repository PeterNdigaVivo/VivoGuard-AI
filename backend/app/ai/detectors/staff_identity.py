"""Shared staff-vs-customer identification (P5 false-alert fix).

A single point-of-truth that the three behind-counter detectors
(StaffPresenceDetector, StaffZoneDetector, UniformComplianceDetector,
plus IntrusionDetector for the after-hours rule) consult before they
decide whether to alert.

Rules (per the P5 ops spec — June 2026):

  HIGH confidence staff (no alerts ever fire):
      correct uniform colour + visible lanyard, OR
      correct uniform colour + in staff/counter zone ≥ 5 min

  MEDIUM confidence staff (intrusion / unauthorised-person alerts
  suppressed; uniform-violation alerts still fire if the colour is
  wrong):
      in staff_zone OR counter zone ≥ 5 min  (any clothing)

  UNKNOWN:
      anything else — too new in zone, OR no uniform read.
      Customer-in-staff-area alerts fire after 2 min in staff_zone
      without uniform.

The module owns a small process-local dict of per-(camera, track)
zone-entry timestamps. Every detector that sees a person in a
staff/counter zone calls `observe()`; the module trims tracks that
disappear via `forget_stale()`. classify() returns the level + the
underlying signals so the caller can write the right kind of
suppression / metric.

DB side-effects: when a track is identified as HIGH or MEDIUM staff
we upsert a row in `staff_tracks` so the analytics endpoints exclude
the visitor from the customer count (existing LEFT-JOIN behaviour).
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date as _date, datetime, timezone
from typing import Literal

from app.ai.detectors.base import COCO_PERSON, DetectorContext
from app.ai.detectors.uniform_compliance import uniform_features
from app.ai.zone_logic import bbox_in_zone, iou


# Tags treated as "staff-only" floor area.
STAFF_ZONE_TAGS = ("staff_zone", "counter")
# How long in a staff/counter zone before time-alone makes the person
# staff regardless of what they're wearing (spec rule 1a).
TIME_BASED_STAFF_SECONDS = 5 * 60
# How long with the wrong uniform before we'll trust that the person
# actually works there (spec rule 4 + rule 8).
WRONG_UNIFORM_CONFIRM_SECONDS = 5 * 60
# After this many seconds without seeing a track we drop the bookkeeping.
TRACK_TTL_SECONDS = 15 * 60


Level = Literal["high", "medium", "uncertain", "unknown"]

# Below this uniform_features confidence we can't reliably tell what
# colour the top is — typical for overhead cameras and dim shop
# lighting. Suppress unauthorised-person alerts so the operator
# doesn't get false "intruder behind counter" pings for staff the
# camera simply can't read.
UNCERTAIN_CONFIDENCE = 0.40


# (camera_id, track_id) → {zone_tag: first_seen_epoch}
_zone_first_seen: dict[tuple[int, int], dict[str, float]] = {}
# (camera_id, track_id) → last_seen_epoch — for stale eviction.
_last_seen: dict[tuple[int, int], float] = {}
# In-memory dedup for staff_tracks DB upserts (per worker process).
_staff_marked: set[tuple[int, str, str]] = set()


def observe(camera_id: int, track_id: int, zone_tag: str, now: float) -> None:
    """Record that this track is currently in `zone_tag`. Idempotent —
    the first observation per (track, zone) is the one that sticks."""
    key = (int(camera_id), int(track_id))
    book = _zone_first_seen.setdefault(key, {})
    book.setdefault(zone_tag, now)
    _last_seen[key] = now


def left_zone(camera_id: int, track_id: int, zone_tag: str) -> None:
    """The track is no longer in `zone_tag`. Reset that one timer so
    the duration clock restarts if they come back."""
    key = (int(camera_id), int(track_id))
    book = _zone_first_seen.get(key)
    if book:
        book.pop(zone_tag, None)


def forget_stale(now: float) -> None:
    """Drop tracks not seen for TRACK_TTL_SECONDS. Called periodically
    from any of the detectors at the end of evaluate()."""
    cutoff = now - TRACK_TTL_SECONDS
    stale = [k for k, ts in _last_seen.items() if ts < cutoff]
    for k in stale:
        _zone_first_seen.pop(k, None)
        _last_seen.pop(k, None)


def time_in_zone(camera_id: int, track_id: int, zone_tag: str,
                 now: float) -> float:
    first = _zone_first_seen.get((int(camera_id), int(track_id)), {}).get(zone_tag)
    if first is None:
        return 0.0
    return max(0.0, now - first)


def time_in_any_staff_zone(camera_id: int, track_id: int, now: float) -> float:
    book = _zone_first_seen.get((int(camera_id), int(track_id)), {})
    best = 0.0
    for tag in STAFF_ZONE_TAGS:
        first = book.get(tag)
        if first is None:
            continue
        best = max(best, now - first)
    return best


def classify(ctx: DetectorContext, det: dict, track_id: int,
             now: float) -> dict:
    """Return the staff-classification verdict for one detection.
    Keys:
      level                 'high' | 'medium' | 'unknown'
      top_ok                bool | None        — correct uniform colour
      has_lanyard           bool | None
      has_nametag           bool | None
      time_in_staff_zone_s  float              — max across staff_zone + counter
      reason                str                — debug breadcrumb
    """
    feats = uniform_features(ctx.frame_bgr, det["bbox_norm"])
    top_ok      = bool(feats["top_ok"])      if feats else None
    has_lanyard = bool(feats["has_lanyard"]) if feats else None
    has_nametag = bool(feats["has_nametag"]) if feats else None
    confidence  = float(feats.get("confidence", 0.0)) if feats else 0.0
    elapsed = time_in_any_staff_zone(ctx.camera_id, track_id, now)

    # HIGH confidence: correct uniform AND (lanyard visible OR long dwell)
    if top_ok and (has_lanyard or elapsed >= TIME_BASED_STAFF_SECONDS):
        return {"level": "high", "top_ok": top_ok,
                "has_lanyard": has_lanyard, "has_nametag": has_nametag,
                "confidence": confidence,
                "time_in_staff_zone_s": elapsed,
                "reason": "uniform+lanyard" if has_lanyard else "uniform+dwell"}

    # MEDIUM confidence: time alone (rule 1a) — uniform unknown / not read.
    if elapsed >= TIME_BASED_STAFF_SECONDS:
        return {"level": "medium", "top_ok": top_ok,
                "has_lanyard": has_lanyard, "has_nametag": has_nametag,
                "confidence": confidence,
                "time_in_staff_zone_s": elapsed, "reason": "dwell≥5min"}

    # MEDIUM confidence: uniform colour but not yet long enough for HIGH.
    # We still trust the colour rule to suppress intrusion-style alerts,
    # but uniform-violation alerts only fire after the dwell threshold.
    if top_ok:
        return {"level": "medium", "top_ok": top_ok,
                "has_lanyard": has_lanyard, "has_nametag": has_nametag,
                "confidence": confidence,
                "time_in_staff_zone_s": elapsed, "reason": "uniform-only"}

    # UNCERTAIN: pixels unreadable (frame missed, occluded, overhead
    # angle, low light). Distinct from "unknown" — the caller treats
    # uncertain as "no alert" so we don't false-alarm on a uniform
    # the camera literally can't see clearly.
    if feats is None or confidence < UNCERTAIN_CONFIDENCE:
        return {"level": "uncertain", "top_ok": top_ok,
                "has_lanyard": has_lanyard, "has_nametag": has_nametag,
                "confidence": confidence,
                "time_in_staff_zone_s": elapsed,
                "reason": "low-confidence-pixels"}

    return {"level": "unknown", "top_ok": top_ok,
            "has_lanyard": has_lanyard, "has_nametag": has_nametag,
            "confidence": confidence,
            "time_in_staff_zone_s": elapsed, "reason": "no-uniform"}


def match_track(ctx: DetectorContext, det: dict) -> int:
    """Find the tracker id whose bbox best matches this detection. Fall
    back to a deterministic id when tracking missed the frame."""
    for tr, _ in ctx.tracks:
        if tr.cls in COCO_PERSON and iou(tr.bbox_norm, det["bbox_norm"]) > 0.3:
            return tr.track_id
    return hash(tuple(round(c, 2) for c in det["bbox_norm"])) & 0x7FFFFFFF


def mark_staff_track(ctx: DetectorContext, track_id: int,
                     source: str = "uniform") -> None:
    """Upsert the staff_tracks roster row for today so analytics
    excludes this person from visitor counts. Best-effort — never
    raises. `source` is a short tag for the dashboard ('uniform',
    'zone', 'opening_closing')."""
    if ctx.db is None or ctx.store_id is None:
        return
    today = _date.today()
    signature = f"cam{ctx.camera_id}:tr{track_id}"
    key = (ctx.store_id, today.isoformat(), signature)
    if key in _staff_marked:
        return
    _staff_marked.add(key)
    try:
        from app.models import StaffTrack
        row = (ctx.db.query(StaffTrack)
                   .filter(StaffTrack.store_id == ctx.store_id,
                           StaffTrack.day == today,
                           StaffTrack.track_signature == signature)
                   .first())
        now_utc = datetime.now(timezone.utc)
        if row is None:
            ctx.db.add(StaffTrack(
                store_id=ctx.store_id, day=today,
                track_signature=signature,
                first_seen=now_utc, last_seen=now_utc,
                classified_as="staff", source=source,
            ))
        else:
            row.last_seen = now_utc
            row.classified_as = "staff"
            row.source = source
        ctx.db.flush()
    except Exception:
        try: ctx.db.rollback()
        except Exception: pass


def is_in_staff_or_counter_zone(ctx: DetectorContext, bbox_norm) -> tuple[bool, str | None]:
    """(matched, tag). matched=True when bbox falls inside any
    staff_zone or counter zone. tag is the matched detection-type
    tag, useful for which timer to update."""
    for z in ctx.zones:
        tags = set(z.get("detection_types_json") or [])
        if not (tags & set(STAFF_ZONE_TAGS)):
            continue
        if bbox_in_zone(bbox_norm, z.get("polygon_coords_json")):
            for t in STAFF_ZONE_TAGS:
                if t in tags:
                    return True, t
    return False, None
