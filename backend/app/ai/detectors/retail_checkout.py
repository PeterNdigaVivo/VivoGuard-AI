"""CheckoutDwellDetector — per-customer transaction duration at counter zones.

A "checkout dwell" is the elapsed time between a track first appearing
inside a counter zone and last leaving it. It approximates a single
transaction at the till.

Sessions are filtered by the per-store thresholds in `settings`:

    settings.checkout_min_dwell_seconds  (default 30s)
    settings.checkout_max_dwell_seconds  (default 900s = 15 min)

Anything shorter than `min` is treated as a pass-through (staff walking
past, tracker glitches). Anything longer than `max` is treated as
not-a-transaction — that's a staff member working the counter for an
entire shift, which is what `staff_present` already measures.

A short-lived Redis key `vg:checkout_open:{cam}:{zone}:{track}` is
written on entry and deleted on exit so the alerting layer can detect
live sessions exceeding `settings.checkout_alert_minutes` WITHOUT
needing to share the detector's in-memory state across processes.

Co-exists with StaffPresenceDetector — same `counter` zone tag,
different output metric. The two detectors don't share state.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Any

from app.ai.detectors.base import COCO_PERSON, DetectionEvent, Detector, DetectorContext
from app.ai.zone_logic import bbox_in_zone

log = logging.getLogger(__name__)

# `vg:checkout_open:{cam}:{zone}:{track}` → JSON {entry_ts, store_id}
REDIS_OPEN_KEY_FMT = "vg:checkout_open:{cam}:{zone}:{track}"
# Key expires automatically so the alerting beat task never picks up
# stale rows from a crashed worker. Set generously above the max
# session length.
REDIS_OPEN_TTL_SECONDS = 30 * 60       # 30 min

# Time after which a track that's been "in the zone" but not seen on any
# frame is considered to have exited (tracker dropouts happen). Tighter
# than REDIS_OPEN_TTL because we still want a timely session-close.
TRACK_LOST_GRACE_SECONDS = 60.0


def _redis():
    """Optional Redis. Detector still emits metric_snapshots when Redis
    is unavailable; only the live-alert path needs the key set."""
    try:
        import redis
        from app.config import settings
        return redis.from_url(settings.redis_url)
    except Exception:
        return None


class CheckoutDwellDetector(Detector):
    """Per-track-per-counter-zone session timer.

    On every evaluate():
      • For each (track, counter-zone) pair where the centroid is now
        IN the zone — if this is the first sighting, record entry +
        publish a Redis open-session key.
      • For each previously-open session whose track is NOT inside any
        counter zone this frame OR whose track hasn't been seen for >
        TRACK_LOST_GRACE_SECONDS, close the session: emit a
        DetectionEvent + write `checkout_dwell_seconds` to
        metric_snapshots (filtered by the min/max thresholds).
      • Periodically sweep expired Redis open-session keys (the TTL
        handles abandoned-by-crashed-worker rows).
    """

    detection_type = "checkout_dwell"
    needs_tracking = True

    def __init__(self) -> None:
        # (camera_id, track_id, zone_id) → {entry_ts, last_seen_ts, store_id}
        self._sessions: dict[tuple[int, int, int], dict[str, Any]] = {}
        # Per-camera "last evaluate" tick — used to GC sessions whose
        # tracks have aged out without our seeing an explicit exit.
        self._last_tick: dict[int, float] = {}

    # ------------------------------------------------------------------

    def _thresholds(self) -> tuple[float, float]:
        from app.config import settings
        lo = float(getattr(settings, "checkout_min_dwell_seconds", 30))
        hi = float(getattr(settings, "checkout_max_dwell_seconds", 900))
        return lo, hi

    def _publish_open(self, r, cam: int, zone: int, track: int,
                       entry_ts: float, store_id: int | None) -> None:
        if r is None:
            return
        try:
            r.set(
                REDIS_OPEN_KEY_FMT.format(cam=cam, zone=zone, track=track),
                json.dumps({
                    "entry_ts":  float(entry_ts),
                    "store_id":  store_id,
                    "camera_id": cam,
                    "zone_id":   zone,
                    "track_id":  track,
                }),
                ex=REDIS_OPEN_TTL_SECONDS,
            )
        except Exception as e:
            log.debug("checkout: publish-open failed: %s", e)

    def _clear_open(self, r, cam: int, zone: int, track: int) -> None:
        if r is None:
            return
        try:
            r.delete(REDIS_OPEN_KEY_FMT.format(cam=cam, zone=zone, track=track))
        except Exception:
            pass

    def _close_session(self, ctx: DetectorContext, key: tuple[int, int, int],
                       exit_ts: float, r) -> DetectionEvent | None:
        sess = self._sessions.pop(key, None)
        if sess is None:
            return None
        cam, track, zone = key
        self._clear_open(r, cam, zone, track)
        duration = float(exit_ts - sess["entry_ts"])
        lo, hi = self._thresholds()
        if duration < lo:
            # Pass-through — staff walking past, tracker glitch, etc.
            return None
        if duration > hi:
            # Staff working the counter for hours — not a checkout
            # transaction. `staff_present` measures this separately.
            log.info("checkout: dropped over-long session cam=%s zone=%s "
                     "track=%s duration=%.0fs (> %.0fs)",
                     cam, zone, track, duration, hi)
            return None

        # Write the metric as one distinct row per session using `dims`
        # so multiple completions in the same 1-minute bucket don't
        # overwrite each other under aggregator="last".
        if ctx.db is not None:
            from app.analytics import recorder
            recorder.record(
                ctx.db, "checkout_dwell_seconds", duration,
                camera_id=cam, store_id=sess.get("store_id"),
                zone_id=zone,
                dims={"session_id": f"{track}:{int(sess['entry_ts'])}"},
                aggregator="last",
            )

        return DetectionEvent(
            detection_type=self.detection_type,
            cls="checkout_complete",
            confidence=1.0,
            bbox_norm=[0.0, 0.0, 1.0, 1.0],
            track_id=track,
            zone_id=zone,
            extra={
                "dwell_seconds": round(duration, 1),
                "entry_ts":      sess["entry_ts"],
                "exit_ts":       exit_ts,
                "store_id":      sess.get("store_id"),
            },
        )

    # ------------------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        # The till point ONLY. A zone qualifies when it is tagged
        # `counter` AND is not tagged with anything that would make
        # a long presence there normal:
        #   - `staff_zone` / `staff_area`: staff working behind the
        #     counter all shift is `staff_present`, not a transaction
        #   - `queue`: queue-wait-time is QueueDetector's job
        # The legacy operator-drawn "counter" zones with no modifier
        # tags are the only ones we want to time.
        EXCLUDE_TAGS = {"staff_zone", "staff_area", "queue"}
        counter_zones = [
            z for z in ctx.zones
            if "counter" in (z.get("detection_types_json") or [])
            and not (EXCLUDE_TAGS & set(z.get("detection_types_json") or []))
        ]
        if not counter_zones:
            return []
        now = ctx.timestamp or time.time()
        self._last_tick[ctx.camera_id] = now
        r = _redis()
        out: list[DetectionEvent] = []

        # Person tracks only — exclude non-person classes early.
        person_tracks = [
            (tr, det) for (tr, det) in (ctx.tracks or [])
            if det.get("cls") in COCO_PERSON
        ]

        # Pass 1 — observe current frame, open new sessions, refresh
        # last_seen_ts on continuing ones.
        active_keys: set[tuple[int, int, int]] = set()
        for tr, det in person_tracks:
            track_id = getattr(tr, "track_id", None)
            if track_id is None:
                continue
            bbox = det.get("bbox_norm")
            if not bbox:
                continue
            for z in counter_zones:
                poly = z.get("polygon_coords_json")
                if not poly:
                    continue
                if not bbox_in_zone(bbox, poly):
                    continue
                key = (ctx.camera_id, int(track_id), int(z["id"]))
                active_keys.add(key)
                sess = self._sessions.get(key)
                if sess is None:
                    sess = {"entry_ts": now, "last_seen_ts": now,
                            "store_id": ctx.store_id}
                    self._sessions[key] = sess
                    self._publish_open(r, ctx.camera_id, int(z["id"]),
                                        int(track_id), now, ctx.store_id)
                    log.debug("checkout: open cam=%s zone=%s track=%s",
                              ctx.camera_id, z["id"], track_id)
                else:
                    sess["last_seen_ts"] = now

        # Pass 2 — close any session whose track is no longer in its
        # zone OR has aged out beyond TRACK_LOST_GRACE_SECONDS. We
        # only inspect sessions belonging to THIS camera — other
        # cameras' sessions are owned by their own detector instances.
        to_close: list[tuple[tuple[int, int, int], float]] = []
        for key, sess in list(self._sessions.items()):
            cam, _track, _zone = key
            if cam != ctx.camera_id:
                continue
            if key in active_keys:
                continue        # still present
            if (now - sess["last_seen_ts"]) >= TRACK_LOST_GRACE_SECONDS:
                to_close.append((key, sess["last_seen_ts"]))
        for key, exit_ts in to_close:
            ev = self._close_session(ctx, key, exit_ts, r)
            if ev is not None:
                out.append(ev)

        return out
