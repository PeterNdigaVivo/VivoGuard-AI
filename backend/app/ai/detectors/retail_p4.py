"""Priority-4 retail detectors: passersby + window engagement.

  - PassersbyDetector
      Counts unique person-tracks that traverse a 'sidewalk' zone.
      Writes the `passersby` metric (1 per new track per minute bucket).

  - WindowEngagementDetector
      For each person-track inside a 'window' zone, tracks dwell time;
      when the track leaves, classifies it as a 'stop' (dwell ≥
      stop_threshold_seconds) or a 'walkby'. Writes:
        passersby                   (every new track in window zone)
        stop_rate                   (stops / passersby, 1-min bucket)
        stop_engagement_seconds     (avg stop dwell in seconds)

  Pair these with the Campaign Analytics endpoint to compute lift
  (before-vs-during-vs-after a campaign date range).
"""
from __future__ import annotations
import time

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone


class PassersbyDetector(Detector):
    """Counts unique person-tracks crossing a 'sidewalk' zone."""

    detection_type = "passersby"
    needs_tracking = True

    def __init__(self):
        self._seen: dict[tuple[int, int, int], float] = {}   # (cam, track, zone) → first-seen

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled") or ctx.db is None:
            return []
        zones = [z for z in ctx.zones if "sidewalk" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        from app.analytics import recorder
        new_count = 0
        for tr, _ in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in zones:
                if not bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    continue
                key = (ctx.camera_id, tr.track_id, z["id"])
                if key in self._seen:
                    continue
                self._seen[key] = time.time()
                new_count += 1
        if new_count > 0:
            recorder.record(ctx.db, "passersby", float(new_count),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            aggregator="sum")
        return []


class WindowEngagementDetector(Detector):
    """Stop-rate + avg engagement time at a 'window' display zone.

    A track that dwells ≥ `stop_threshold_seconds` is counted as a stop.
    Track exits emit a per-track event so the operator can browse the
    longest stops as 'engaged visitors'.
    """

    detection_type = "window_engagement"
    needs_tracking = True

    def __init__(self):
        # (cam, track, zone) → t0 entered
        self._entered: dict[tuple[int, int, int], float] = {}
        # Within the current 1-minute bucket: counts of passersby & stops
        self._bucket_seen: dict[int, int] = {}    # zone_id → unique tracks this bucket
        self._bucket_stops: dict[int, int] = {}
        self._bucket_start: float = time.time()
        self._seen_keys: set[tuple[int, int, int]] = set()

    def _flush_bucket(self, ctx: DetectorContext) -> None:
        """Push the just-elapsed minute's stop-rate metric to Redis/DB."""
        if not self._bucket_seen or ctx.db is None:
            return
        from app.analytics import recorder
        for zid, seen in self._bucket_seen.items():
            stops = self._bucket_stops.get(zid, 0)
            rate  = (stops / seen) if seen else 0.0
            recorder.record(ctx.db, "stop_rate", rate,
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            zone_id=zid, aggregator="last")
        self._bucket_seen.clear()
        self._bucket_stops.clear()
        self._seen_keys.clear()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        stop_thr = int((cfg.get("extra") or {}).get("stop_threshold_seconds", 3))
        zones = [z for z in ctx.zones if "window" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        now = time.time()
        if now - self._bucket_start >= 60:
            self._flush_bucket(ctx)
            self._bucket_start = now

        out: list[DetectionEvent] = []
        active = {tr.track_id for tr, _ in ctx.tracks if tr.cls in COCO_PERSON}

        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in zones:
                key = (ctx.camera_id, tr.track_id, z["id"])
                inside = bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"])
                if inside:
                    self._entered.setdefault(key, now)
                    if key not in self._seen_keys:
                        self._seen_keys.add(key)
                        self._bucket_seen[z["id"]] = self._bucket_seen.get(z["id"], 0) + 1
                else:
                    if key in self._entered:
                        dwell = now - self._entered.pop(key)
                        is_stop = dwell >= stop_thr
                        if is_stop and ctx.db is not None:
                            self._bucket_stops[z["id"]] = self._bucket_stops.get(z["id"], 0) + 1
                            from app.analytics import recorder
                            recorder.record(ctx.db, "stop_engagement_seconds", dwell,
                                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                                            zone_id=z["id"], aggregator="avg")
                            out.append(DetectionEvent(
                                detection_type=self.detection_type, cls="stop",
                                confidence=1.0, bbox_norm=tr.bbox_norm,
                                track_id=tr.track_id, zone_id=z["id"],
                                extra={"dwell_seconds": round(dwell, 1),
                                       "store_id": ctx.store_id},
                            ))

        # Reap dropped tracks.
        for k in list(self._entered):
            if k[1] not in active:
                self._entered.pop(k, None)
        return out
