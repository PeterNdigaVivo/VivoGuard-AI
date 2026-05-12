"""Priority-1 retail detectors:

  - QueueDetector         — counts people in queue-tagged zones,
                            writes queue_length + queue_wait_seconds metrics,
                            fires an alert when the queue exceeds threshold
  - OccupancyMetricsDetector — extends the existing OccupancyDetector
                            with metric writes + capacity-based alerts
  - UniqueVisitorDetector — entry-zone-driven daily dedup of visitors
  - IntrusionDetector     — person/motion outside business hours in a
                            restricted zone → high-priority alert

All four use `ctx.db` for metric writes; without a DB session attached
they degrade gracefully (events still fire, no metrics persisted).
"""
from __future__ import annotations
import time
from datetime import date, datetime, timezone

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone
from app.utils.business_hours import is_open, localised_now


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class QueueDetector(Detector):
    """Counts people inside any zone tagged `queue` and writes the bucket
    metric. Optionally fires an alert when count >= `crowd_threshold`.

    Wait time is computed by the tracker: for any person track that
    sits inside the queue zone, the dwell duration is averaged and
    pushed to `queue_wait_seconds`.
    """

    detection_type = "queue"
    needs_tracking = True

    def __init__(self):
        self._entered: dict[tuple[int, int, int], float] = {}   # (cam, track, zone) → t0
        self._fired:   dict[int, float] = {}                    # zone_id → last alert ts

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        threshold = int(cfg.get("crowd_threshold") or 5)
        thr_conf  = float(cfg.get("confidence_threshold", 0.5))
        zones = [z for z in ctx.zones
                 if "queue" in (z.get("detection_types_json") or [])
                 and not z.get("suppressed")]
        if not zones:
            return []

        # Persons crossing confidence — used both for the headcount and
        # the per-track dwell measurement.
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]

        out: list[DetectionEvent] = []
        now = time.time()
        for z in zones:
            in_zone = [p for p in people if bbox_in_zone(p["bbox_norm"], z["polygon_coords_json"])]
            count = len(in_zone)

            # Track-level dwell to estimate wait time.
            track_dwells: list[float] = []
            for tr, _det in ctx.tracks:
                if tr.cls not in COCO_PERSON:
                    continue
                if not bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    self._entered.pop((ctx.camera_id, tr.track_id, z["id"]), None)
                    continue
                key = (ctx.camera_id, tr.track_id, z["id"])
                t0 = self._entered.setdefault(key, now)
                track_dwells.append(max(0.0, now - t0))

            avg_wait = (sum(track_dwells) / len(track_dwells)) if track_dwells else 0.0

            # Metric writes (best effort).
            if ctx.db is not None:
                from app.analytics import recorder
                recorder.record(ctx.db, "queue_length", float(count),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=z["id"], aggregator="max")
                if track_dwells:
                    recorder.record(ctx.db, "queue_wait_seconds", avg_wait,
                                    camera_id=ctx.camera_id, store_id=ctx.store_id,
                                    zone_id=z["id"], aggregator="avg")

            # Alert promotion — dedup per zone, every 60s max.
            if count >= threshold and now - self._fired.get(z["id"], 0) > 60:
                self._fired[z["id"]] = now
                # Use the densest person's bbox as the alert thumbnail target.
                anchor = max(in_zone, key=lambda p: p["conf"])
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="queue",
                    confidence=min(1.0, count / max(threshold, 1)),
                    bbox_norm=anchor["bbox_norm"], zone_id=z["id"],
                    extra={"count": count, "threshold": threshold,
                           "avg_wait_seconds": round(avg_wait, 1)},
                ))
        return out


# ---------------------------------------------------------------------------
# Occupancy extension — adds metric writes + store-capacity alert
# ---------------------------------------------------------------------------

class OccupancyMetricsDetector(Detector):
    """Companion to the existing OccupancyDetector — only writes
    metrics + capacity alerts. The original OccupancyDetector keeps
    handling per-zone crowd events; this one handles the *store-wide*
    occupancy KPI used by the dashboard.
    """

    detection_type = "occupancy_metrics"

    def __init__(self):
        self._fired_cap = False
        self._fired_cap_at = 0.0

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get("occupancy")    # piggybacks on the operator's existing config
        if not cfg or not cfg.get("enabled"):
            return []
        thr_conf = float(cfg.get("confidence_threshold", 0.5))
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        count = len(people)

        if ctx.db is not None:
            from app.analytics import recorder
            recorder.record(ctx.db, "occupancy", float(count),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            aggregator="last")

        # Capacity alert — once per minute when exceeded.
        out: list[DetectionEvent] = []
        if ctx.store_id is not None and ctx.db is not None:
            from app.models import Store
            store = ctx.db.get(Store, ctx.store_id)
            cap = (store.capacity if store else None) if store else None
            now = time.time()
            if cap and count >= cap and now - self._fired_cap_at > 60:
                self._fired_cap_at = now
                out.append(DetectionEvent(
                    detection_type="occupancy", cls="capacity",
                    confidence=min(1.0, count / cap), bbox_norm=[0, 0, 1, 1],
                    extra={"count": count, "capacity": cap, "store_id": ctx.store_id},
                ))
        return out


# ---------------------------------------------------------------------------
# Unique visitors — entry-zone driven daily dedup
# ---------------------------------------------------------------------------

class UniqueVisitorDetector(Detector):
    """Marks a new visitor each time a person track is seen for the
    first time today in an `entry`-tagged zone. Dedup is at the (store,
    day, camera, track_id) level — good enough without a ReID model;
    swap the signature builder when a ReID model is wired in.

    No alert is emitted; the metric lands in `visitor_tracks` and is
    read by the dashboard.
    """

    detection_type = "unique_visitor"
    needs_tracking = True

    def __init__(self):
        self._seen_today: set[tuple] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        if ctx.db is None:
            return []
        zones = [z for z in ctx.zones if "entry" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        today = date.today()
        from app.models import VisitorTrack

        for tr, det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            # Must be inside any entry zone.
            in_entry = False
            for z in zones:
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    in_entry = True
                    break
            if not in_entry:
                continue

            signature = f"cam{ctx.camera_id}:tr{tr.track_id}"
            cache_key = (ctx.store_id, today, signature)
            if cache_key in self._seen_today:
                continue
            self._seen_today.add(cache_key)

            # INSERT … ON CONFLICT DO NOTHING (we have a unique index on
            # (store_id, day, track_signature)).
            try:
                vt = VisitorTrack(
                    store_id=ctx.store_id,
                    camera_id=ctx.camera_id,
                    day=today,
                    track_signature=signature,
                )
                ctx.db.add(vt)
                ctx.db.flush()
            except Exception:
                ctx.db.rollback()
        return []


# ---------------------------------------------------------------------------
# Intrusion — outside-business-hours person/motion in restricted zones
# ---------------------------------------------------------------------------

class IntrusionDetector(Detector):
    """Fires a *high-priority* DetectionEvent when a person is seen
    inside a `restricted` zone while the store is closed.

    Closed = `is_open(store.business_hours, local_now) == False`.
    Without a store assignment we fall back to a global
    `extra.always_armed` flag (useful for back-alley cameras).
    """

    detection_type = "intrusion"

    def __init__(self):
        self._fired: dict[int, float] = {}    # zone_id → last alert ts

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        # Determine whether the store is closed right now.
        if ctx.business_hours is not None and ctx.store_timezone:
            local_now = localised_now(ctx.store_timezone)
            closed = not is_open(ctx.business_hours, local_now)
        else:
            closed = bool((cfg.get("extra") or {}).get("always_armed"))
        if not closed:
            return []

        thr = float(cfg.get("confidence_threshold", 0.5))
        zones = [z for z in ctx.zones if "restricted" in (z.get("detection_types_json") or [])]
        if not zones:
            # No restricted zones defined → treat the entire frame as armed.
            zones = [{"id": None, "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]],
                      "detection_types_json": ["restricted"], "suppressed": False}]

        out: list[DetectionEvent] = []
        now = time.time()
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON or det["conf"] < thr:
                continue
            for z in zones:
                if bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"]):
                    zid = z.get("id") or -1
                    if now - self._fired.get(zid, 0) < 30:
                        continue
                    self._fired[zid] = now
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls="person",
                        confidence=det["conf"], bbox_norm=det["bbox_norm"],
                        zone_id=z.get("id"),
                        extra={"priority": "high", "store_id": ctx.store_id,
                               "after_hours": True},
                    ))
                    break
        return out
