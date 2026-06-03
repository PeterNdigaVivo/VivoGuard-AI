"""Uniform compliance detector for Vivo Fashion Group staff.

Two modes, model-first then rule-based fallback:

1. Custom-model mode: if the assigned model emits class labels
   `uniform_ok` / `uniform_violation` / `no_lanyard` / `civilian`,
   the highest-confidence detection per person wins.

2. Rule-based mode (works immediately, no training): for each person
   standing in a `counter` / `staff` zone, analyse the upper-body
   crop in HSV —
     • correct uniform colour: Vivo black OR burgundy/maroon top
     • lanyard: orange or dark vertical strap across the chest
     • name tag: bright white rectangle on the chest
   and combine into a compliance score:
        0.50 * correct_colour + 0.30 * has_lanyard + 0.20 * has_nametag
     score >= 0.80          → uniform_ok
     0.50 <= score < 0.80   → no_lanyard (partial — usually a missing tag)
     score < 0.50           → uniform_violation

People in non-staff zones are treated as civilians and skipped — we
only score staff who should be in uniform.

Writes the `uniform_compliance_pct` metric (rolling avg, 1.0 ok /
0.5 partial / 0.0 violation). Alerts:
  • violation sustained > 2 min  → "staff uniform violation" (warning)
  • no lanyard sustained > 5 min → "staff missing name tag"  (info)
  • > 3 violations in a day      → "repeated uniform violations" (warning)
Dedup: same track + same violation, max once per 30 min.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone, iou


# Compliance state constants — also the alert `cls` strings.
# Spec P5: six-state classifier.
#   FULL_COMPLIANT    correct top + lanyard + nametag        → no alert
#   PARTIAL_COMPLIANT correct top + lanyard, no nametag      → ATTENTION 5min
#   COLOR_ONLY        correct top only                       → ATTENTION 5min (folded with partial)
#   NON_COMPLIANT     wrong colour / no uniform top          → URGENT 2min
#   CUSTOMER          not in staff zone                      → skip
#   UNCERTAIN         can't tell (low light / occluded)      → skip
FULL_COMPLIANT    = "full_compliant"
PARTIAL_COMPLIANT = "partial_compliant"
COLOR_ONLY        = "color_only"
NON_COMPLIANT     = "non_compliant"
CIVILIAN          = "customer"
UNCERTAIN         = "uncertain"

# Legacy aliases kept so older callers / cached UI bundles keep working.
OK         = FULL_COMPLIANT       # uniform_ok
NO_LANYARD = PARTIAL_COMPLIANT    # no_lanyard
VIOLATION  = NON_COMPLIANT        # uniform_violation

_METRIC = {
    FULL_COMPLIANT:    1.0,
    PARTIAL_COMPLIANT: 0.7,
    COLOR_ONLY:        0.5,
    NON_COMPLIANT:     0.0,
}

# Sustained-duration thresholds (seconds) before an alert fires.
VIOLATION_SECONDS  = 2 * 60       # NON_COMPLIANT  → URGENT
NO_LANYARD_SECONDS = 5 * 60       # PARTIAL/COLOR  → ATTENTION
# Per (track, kind) dedup window.
DEDUP_SECONDS = 30 * 60
# Repeated-violations-today threshold.
REPEAT_THRESHOLD = 3

STAFF_ZONE_TAGS = {"counter", "staff"}


def uniform_features(frame_bgr, bbox_norm) -> dict | None:
    """Vivo P5 colour analysis for one person bbox. Returns a dict
    {top_ok, has_lanyard, has_nametag, confidence} or None if pixels
    can't be read. Uses the spec's exact HSV bands.

    UPPER BODY (top 0-50% of bbox):
      • MAROON  H in [160..179] or [0..5], S 50-100% (= 128-255 on 0..255),
                                            V 30-80% (= 76-204)
      • BLACK   any H, S 0-40% (0-102), V 0-60% (0-153)
    LANYARD ZONE (top 15-65%, centre 30-70% of width):
      • orange: H 5..25 (≈10-50° / 2), S 70-100% (179-255), V 70-100% (179-255)
      • black : low V (<128), any H
    NAMETAG ZONE (top 20-60%, centre 20-80% of width):
      • white rectangle: S < 30% (<77), V > 70% (>179)
    """
    try:
        import numpy as np  # noqa: F401
        import cv2
    except ImportError:
        return None
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0 or not bbox_norm:
        return None
    try:
        h, w = frame_bgr.shape[:2]
        x1, y1, x2, y2 = bbox_norm
        px1, py1 = int(max(0, x1) * w), int(max(0, y1) * h)
        px2, py2 = int(min(1, x2) * w), int(min(1, y2) * h)
        if px2 <= px1 or py2 <= py1:
            return None
        bh = py2 - py1
        bw = px2 - px1

        # Upper body (top 0..50% of bbox) for the top colour decision.
        ub = frame_bgr[py1:py1 + int(bh * 0.50), px1:px2]
        if ub.size == 0:
            return None
        hsv = cv2.cvtColor(ub, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        total = H.size or 1
        # Spec values, scaled to OpenCV's 0..179 H / 0..255 S,V.
        maroon = (((H >= 160) | (H <= 5)) & (S >= 128) & (S <= 255)
                  & (V >= 76)  & (V <= 204)).sum() / total
        black  = ((S <= 102) & (V <= 153)).sum() / total
        # 25% of the upper-body pixels matching either band is enough
        # to call it the uniform colour — accounts for shadows + skin.
        top_ok = max(maroon, black) >= 0.25
        top_share = float(max(maroon, black))

        # Lanyard zone — top 15..65%, centre 30..70% width.
        lz_y1 = py1 + int(bh * 0.15); lz_y2 = py1 + int(bh * 0.65)
        lz_x1 = px1 + int(bw * 0.30); lz_x2 = px1 + int(bw * 0.70)
        lanyard_zone = frame_bgr[lz_y1:lz_y2, lz_x1:lz_x2]
        has_lanyard = False
        if lanyard_zone.size > 0:
            lhsv = cv2.cvtColor(lanyard_zone, cv2.COLOR_BGR2HSV)
            lH, lS, lV = lhsv[:, :, 0], lhsv[:, :, 1], lhsv[:, :, 2]
            lt = lH.size or 1
            orange = (((lH >= 5) & (lH <= 25) & (lS >= 179) & (lV >= 179))
                      .sum() / lt)
            dark   = (lV < 128).sum() / lt
            has_lanyard = bool(orange >= 0.005 or dark >= 0.08)

        # Nametag zone — top 20..60%, centre 20..80% width. White card.
        nt_y1 = py1 + int(bh * 0.20); nt_y2 = py1 + int(bh * 0.60)
        nt_x1 = px1 + int(bw * 0.20); nt_x2 = px1 + int(bw * 0.80)
        nametag_zone = frame_bgr[nt_y1:nt_y2, nt_x1:nt_x2]
        has_nametag = False
        if nametag_zone.size > 0:
            nhsv = cv2.cvtColor(nametag_zone, cv2.COLOR_BGR2HSV)
            nS, nV = nhsv[:, :, 1], nhsv[:, :, 2]
            nt = nS.size or 1
            white = ((nS < 77) & (nV > 179)).sum() / nt
            has_nametag = bool(white >= 0.015)

        # Confidence proxy — how strongly the top read as a Vivo colour
        # tells the caller whether to trust the call at all (UNCERTAIN
        # fallback when nothing matched cleanly).
        confidence = min(1.0, top_share / 0.45)
        return {
            "top_ok":      bool(top_ok),
            "has_lanyard": has_lanyard,
            "has_nametag": has_nametag,
            "confidence":  confidence,
        }
    except Exception:
        return None


def uniform_colour_score(frame_bgr, bbox_norm) -> float | None:
    """Legacy 0..1 score retained for callers that haven't moved to
    uniform_features() yet (the StaffPresenceDetector colour fallback,
    the staff-track marker). Computed from the new features so the
    two paths agree."""
    feats = uniform_features(frame_bgr, bbox_norm)
    if feats is None:
        return None
    correct = 1.0 if feats["top_ok"] else 0.0
    return (0.50 * correct
            + 0.30 * (1.0 if feats["has_lanyard"] else 0.0)
            + 0.20 * (1.0 if feats["has_nametag"] else 0.0))



class UniformComplianceDetector(Detector):
    detection_type = "uniform_compliance"
    needs_tracking = True

    def __init__(self):
        # (track_id, kind) -> last alert epoch.
        self._fired: dict[tuple[int, str], float] = {}
        # track_id -> (state, since_epoch) for sustained-duration alerts.
        self._state_since: dict[int, tuple[str, float]] = {}
        # store_id -> {"day": iso, "count": n} for the repeat-day rule.
        self._violations_today: dict[int, dict] = {}
        # (store_id, day, signature) already written to staff_tracks —
        # avoids re-querying/writing every frame for the same track.
        self._staff_marked: set[tuple[int, str, str]] = set()
        # Per-track dedup for the auto-crop harvester — capture at most
        # one frame per person per CROP_DEDUP_SECONDS so the training
        # queue doesn't fill with near-duplicates of the same person.
        self._last_cropped: dict[int, float] = {}

    # ---- public ----------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        staff_zones = [z for z in ctx.zones
                       if STAFF_ZONE_TAGS & set(z.get("detection_types_json") or [])]
        # Without a staff/counter zone we have no way to tell staff from
        # customers, so we don't guess — operators must tag the counter.
        if not staff_zones:
            return []

        now = time.time()
        out: list[DetectionEvent] = []
        scored = 0
        ok_like = 0

        # Each person currently standing in a staff zone gets scored.
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON:
                continue
            if not any(bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"])
                       for z in staff_zones):
                continue   # civilian / not at the counter — skip

            state = self._classify(ctx, det, cfg)
            # Skip non-scoreable people — customers (correct: outside
            # staff zones) and UNCERTAIN frames (low light / occluded
            # / too far). Both should NOT raise alerts.
            if state in (CIVILIAN, UNCERTAIN):
                continue
            scored += 1
            # "OK-like" = anyone in the right uniform colour, whether
            # they have the lanyard/tag or not. Used for the rolling
            # compliance metric.
            if state in (FULL_COMPLIANT, PARTIAL_COMPLIANT, COLOR_ONLY):
                ok_like += 1

            tid = self._match_track(ctx, det)
            # Staff exclusion — anyone scoring as uniformed staff (any
            # of the three uniform-present states) is excluded from the
            # visitor / dwell counts via the staff_tracks roster the
            # analytics endpoints LEFT-JOIN against. NON_COMPLIANT
            # stays a potential customer / unidentified person.
            if state in (FULL_COMPLIANT, PARTIAL_COMPLIANT, COLOR_ONLY):
                self._mark_staff_track(ctx, tid)

            evt = self._maybe_alert(ctx, det, tid, state, now)
            if evt is not None:
                out.append(evt)

            # Camera-zone training crop harvest — save a per-person crop
            # to the chain training queue, suggested-label tagged by
            # zone (staff_zone / counter → uniform; entry_exit / queue
            # → customer). 5-min dedup per track keeps the queue from
            # filling with near-duplicates of the same person.
            self._maybe_harvest_crop(ctx, det, tid, state, now)

        # Compliance metric — fraction of scored staff who are at least
        # in the right uniform (ok or no_lanyard count as "in uniform").
        if ctx.db is not None and scored:
            from app.analytics import recorder
            recorder.record(ctx.db, "uniform_compliance_pct",
                            ok_like / scored,
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            aggregator="avg")
        return out

    # ---- classification --------------------------------------------

    def _classify(self, ctx: DetectorContext, det: dict, cfg: dict) -> str:
        """Six-state classification: FULL_COMPLIANT / PARTIAL_COMPLIANT
        / COLOR_ONLY / NON_COMPLIANT / CUSTOMER / UNCERTAIN.

        Model-class first, then the colour rule-based fallback."""
        extra = cfg.get("extra") or {}
        thr = float(cfg.get("confidence_threshold", 0.5))
        # Mode 1: custom model emitting the six (or legacy four) classes.
        labels = {
            FULL_COMPLIANT:    extra.get("class_full_compliant",    "full_compliant"),
            PARTIAL_COMPLIANT: extra.get("class_partial_compliant", "partial_compliant"),
            COLOR_ONLY:        extra.get("class_color_only",        "color_only"),
            NON_COMPLIANT:     extra.get("class_non_compliant",     "non_compliant"),
            CIVILIAN:          extra.get("class_customer",          "customer"),
            UNCERTAIN:         extra.get("class_uncertain",         "uncertain"),
            # Legacy 4-class names kept so older deployed models keep
            # mapping correctly.
            FULL_COMPLIANT:    extra.get("class_ok",        "uniform_ok"),
            PARTIAL_COMPLIANT: extra.get("class_no_lanyard","no_lanyard"),
            NON_COMPLIANT:     extra.get("class_violation", "uniform_violation"),
        }
        best_state, best_conf = None, 0.0
        for state, label in labels.items():
            for d in ctx.raw_detections:
                if d["cls"] == label and d["conf"] >= thr:
                    if iou(d["bbox_norm"], det["bbox_norm"]) > 0.3 and d["conf"] > best_conf:
                        best_state, best_conf = state, d["conf"]
        if best_state is not None:
            return best_state

        # Mode 2: colour rule-based, six-state decision tree.
        feats = uniform_features(ctx.frame_bgr, det["bbox_norm"])
        if feats is None or feats["confidence"] < 0.15:
            # Pixels missing or top didn't match anything cleanly — let
            # the caller skip (no alert) rather than false-alarm.
            return UNCERTAIN
        if not feats["top_ok"]:
            return NON_COMPLIANT
        if feats["has_lanyard"] and feats["has_nametag"]:
            return FULL_COMPLIANT
        if feats["has_lanyard"]:
            return PARTIAL_COMPLIANT
        return COLOR_ONLY

    # ---- staff exclusion -------------------------------------------

    def _mark_staff_track(self, ctx: DetectorContext, tid: int) -> None:
        """Record this track as staff for today so the analytics
        endpoints exclude it from visitor / dwell counts. Writes to the
        same staff_tracks roster the counter-dwell classifier uses,
        with a `cam{cam}:tr{tid}` signature that matches the
        UniqueVisitorDetector's VisitorTrack signature, and a source of
        'uniform' so the dashboard can show uniform- vs zone-identified
        staff separately. Deduped per session via an in-memory set."""
        if ctx.db is None or ctx.store_id is None:
            return
        from datetime import date, datetime, timezone
        today = date.today()
        signature = f"cam{ctx.camera_id}:tr{tid}"
        key = (ctx.store_id, today.isoformat(), signature)
        if key in self._staff_marked:
            return
        self._staff_marked.add(key)
        try:
            from app.models import StaffTrack
            row = (ctx.db.query(StaffTrack)
                       .filter(StaffTrack.store_id == ctx.store_id,
                               StaffTrack.day == today,
                               StaffTrack.track_signature == signature)
                       .first())
            now = datetime.now(timezone.utc)
            if row is None:
                ctx.db.add(StaffTrack(
                    store_id=ctx.store_id, day=today,
                    track_signature=signature,
                    first_seen=now, last_seen=now,
                    classified_as="staff", source="uniform",
                ))
            else:
                row.last_seen = now
                row.classified_as = "staff"
                row.source = "uniform"
            ctx.db.flush()
        except Exception:
            try:
                ctx.db.rollback()
            except Exception:
                pass

    # ---- alerting --------------------------------------------------

    def _match_track(self, ctx: DetectorContext, det: dict) -> int:
        for tr, _ in ctx.tracks:
            if tr.cls in COCO_PERSON and iou(tr.bbox_norm, det["bbox_norm"]) > 0.3:
                return tr.track_id
        # Stable-ish fallback id from the bbox when tracking misses.
        return hash(tuple(round(c, 2) for c in det["bbox_norm"])) & 0x7FFFFFFF

    # ---- training-crop harvest -------------------------------------

    CROP_DEDUP_SECONDS = 5 * 60          # 1 crop / person / 5 min
    PENDING_CAP        = 500             # pause harvest above this
    # Suggested label per source zone tag — spec P1.
    _ZONE_TO_LABEL = {
        "staff_zone": "uniform_ok",
        "counter":    "uniform_ok",
        "queue":      "civilian",
        "entry_exit": "civilian",
    }

    def _maybe_harvest_crop(self, ctx: DetectorContext, det: dict,
                            tid: int, state: str, now: float) -> None:
        """Save a cropped person frame into training_samples (source=
        'camera_crop', approved=null pending review) when this is a
        fresh sighting AND the pending-review queue still has room.
        Best-effort — never raises."""
        if ctx.db is None or ctx.frame_bgr is None:
            return
        if state == UNCERTAIN:
            return   # blurry / unsure — not useful training signal
        if now - self._last_cropped.get(tid, 0) < self.CROP_DEDUP_SECONDS:
            return
        # Decide which zone (if any) contains this detection — that
        # tells us the suggested label.
        zone_tag, crop_kind = self._zone_for_crop(ctx, det["bbox_norm"])
        if zone_tag is None:
            return   # only harvest from the four labelled zones
        try:
            from app.models import TrainingSample
            pending = (ctx.db.query(TrainingSample.id)
                          .filter(TrainingSample.detector_type == "uniform",
                                  TrainingSample.source == "camera_crop",
                                  TrainingSample.approved.is_(None))
                          .count())
        except Exception:
            pending = 0
        if pending >= self.PENDING_CAP:
            return   # back-pressure until the review queue catches up

        # Crop person bbox; for `counter` zones we keep the spec's
        # upper-body 60% crop (better signal for the uniform check).
        path = self._write_crop(ctx, det["bbox_norm"], crop_kind)
        if not path:
            return

        try:
            from datetime import datetime, timezone
            sample = TrainingSample(
                detector_type="uniform",
                label=self._ZONE_TO_LABEL.get(zone_tag, "civilian"),
                camera_id=ctx.camera_id, store_id=ctx.store_id,
                frame_path=path,
                captured_at=datetime.now(timezone.utc),
                source="camera_crop",
                shared=True,
                approved=None,           # pending operator review
            )
            ctx.db.add(sample)
            ctx.db.flush()
            self._last_cropped[tid] = now
        except Exception:
            try: ctx.db.rollback()
            except Exception: pass

    @staticmethod
    def _zone_for_crop(ctx: DetectorContext, bbox_norm) -> tuple[str | None, str]:
        """Find the zone tag (staff_zone / counter / queue / entry_exit)
        the detection sits in. Returns (zone_tag, crop_kind) where
        crop_kind is 'upper' for counter (top 60% of bbox) and 'full'
        otherwise."""
        for z in ctx.zones:
            tags = set(z.get("detection_types_json") or [])
            if not (tags & {"staff_zone", "counter", "queue", "entry_exit"}):
                continue
            if bbox_in_zone(bbox_norm, z.get("polygon_coords_json")):
                if "staff_zone" in tags: return "staff_zone", "full"
                if "counter"    in tags: return "counter",    "upper"
                if "queue"      in tags: return "queue",      "full"
                if "entry_exit" in tags: return "entry_exit", "full"
        return None, "full"

    def _write_crop(self, ctx: DetectorContext, bbox_norm, crop_kind: str) -> str | None:
        """Write the cropped JPEG to /data/training/uniform/_camera_crops/
        and return the on-disk path."""
        try:
            import cv2
            from pathlib import Path
            from datetime import datetime, timezone
            from app.config import settings
            frame = ctx.frame_bgr
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox_norm
            px1, py1 = int(max(0, x1) * w), int(max(0, y1) * h)
            px2, py2 = int(min(1, x2) * w), int(min(1, y2) * h)
            if px2 <= px1 or py2 <= py1:
                return None
            if crop_kind == "upper":
                py2 = py1 + int((py2 - py1) * 0.60)
            crop = frame[py1:py2, px1:px2]
            if crop.size == 0:
                return None
            root = (Path(settings.datasets_dir).parent
                    / "training" / "uniform" / "_camera_crops")
            root.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            path = root / f"cam{ctx.camera_id}_{ts}.jpg"
            cv2.imwrite(str(path), crop)
            return str(path)
        except Exception:
            return None

    def _maybe_alert(self, ctx: DetectorContext, det: dict, tid: int,
                     state: str, now: float) -> DetectionEvent | None:
        # Maintain the sustained-duration timer per track.
        prev = self._state_since.get(tid)
        if prev is None or prev[0] != state:
            self._state_since[tid] = (state, now)
            since = now
        else:
            since = prev[1]
        elapsed = now - since

        if state == NON_COMPLIANT and elapsed >= VIOLATION_SECONDS:
            if now - self._fired.get((tid, NON_COMPLIANT), 0) >= DEDUP_SECONDS:
                self._fired[(tid, NON_COMPLIANT)] = now
                self._bump_violation_count(ctx, now)
                repeated = self._violation_count_today(ctx) > REPEAT_THRESHOLD
                return DetectionEvent(
                    detection_type=self.detection_type, cls=NON_COMPLIANT,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "warning", "store_id": ctx.store_id,
                           "rule": "uniform_violation",
                           "shift": _shift_label(),
                           "repeated_today": repeated},
                )
        # Partial compliance and "right colour but no lanyard" both get
        # the gentle 5-minute INFO nudge — same operator action.
        if state in (PARTIAL_COMPLIANT, COLOR_ONLY) and elapsed >= NO_LANYARD_SECONDS:
            kind = state
            if now - self._fired.get((tid, kind), 0) >= DEDUP_SECONDS:
                self._fired[(tid, kind)] = now
                rule = "no_lanyard" if state == PARTIAL_COMPLIANT else "color_only"
                return DetectionEvent(
                    detection_type=self.detection_type, cls=kind,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "info", "store_id": ctx.store_id,
                           "rule": rule, "shift": _shift_label()},
                )
        return None

    def _bump_violation_count(self, ctx: DetectorContext, now: float) -> None:
        sid = ctx.store_id if ctx.store_id is not None else -1
        today = datetime.now(timezone.utc).date().isoformat()
        rec = self._violations_today.get(sid)
        if rec is None or rec.get("day") != today:
            rec = {"day": today, "count": 0}
        rec["count"] += 1
        self._violations_today[sid] = rec

    def _violation_count_today(self, ctx: DetectorContext) -> int:
        sid = ctx.store_id if ctx.store_id is not None else -1
        rec = self._violations_today.get(sid)
        today = datetime.now(timezone.utc).date().isoformat()
        return rec["count"] if rec and rec.get("day") == today else 0


def _shift_label() -> str:
    h = datetime.now(timezone.utc).hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"
