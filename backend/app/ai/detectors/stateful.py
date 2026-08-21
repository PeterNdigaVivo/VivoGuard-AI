"""Stateful / aggregate detectors.

  - CrowdDetector       — count people, fire when ≥ threshold
  - OccupancyDetector   — same idea per-zone, with throttling
  - LoiteringDetector   — track persists in zone for ≥ dwell seconds
  - AbandonedObject     — non-person object stationary ≥ N seconds
  - TrespassDetector    — any object of relevant class enters polygon
  - TripwireDetector    — track centre crosses a line segment
  - TailgatingDetector  — second person enters a tripwire within window
  - FallDetector        — bbox aspect ratio inversion (heuristic)
  - HeatmapDetector     — accumulator (no events emitted; reads via API)
  - LPRDetector         — vehicle bbox crop → OCR plate (placeholder OCR)
"""
from __future__ import annotations
import time
from datetime import datetime

from app.ai.detectors.base import COCO_PERSON, COCO_VEHICLE, Detector, DetectorContext, DetectionEvent
from app.ai.zone_logic import bbox_centre, bbox_in_zone, iou, segments_cross, zone_contains


class CrowdDetector(Detector):
    """Detect a sustained congregation, not a transient head-count spike.

    A crowd incident is promoted only when enough tracked people have remained
    locally stationary for the configured dwell.  Once promoted, the incident
    stays latched until the area has been below threshold for the re-arm
    period.  This prevents the same ByteTrack identities producing an alert on
    every inference frame.

    ``DetectionConfig.dwell_time_seconds`` is the operator-facing dwell knob.
    Advanced values live in ``extra``:

    * ``max_motion_norm`` (default 0.04) — maximum recent centre displacement;
    * ``incident_rearm_seconds`` (default 60) — continuous clear time required.

    Vivo retail operations defines a crowd as at least six people remaining
    stationary for five minutes.  Per-camera configuration may override both
    values, but the safe fleet default must reflect that reviewed rule.
    """

    detection_type = "crowd"
    needs_tracking = True

    DEFAULT_THRESHOLD = 6
    DEFAULT_DWELL_SECONDS = 5 * 60.0
    DEFAULT_MAX_MOTION_NORM = 0.04
    DEFAULT_REARM_SECONDS = 60.0
    MOTION_WINDOW = 8

    def __init__(self):
        self._stationary_since: dict[tuple[int, int], float] = {}
        self._active_tracks: dict[int, set[int]] = {}
        self._clear_since: dict[int, float] = {}

    @classmethod
    def _locally_stationary(cls, track, max_motion: float) -> bool:
        history = list(track.history or [])[-cls.MOTION_WINDOW:]
        if len(history) < 3:
            return False
        centres = [bbox_centre(bb) for bb in history]
        x0, y0 = centres[0]
        return max(
            ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            for x, y in centres[1:]
        ) <= max_motion

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        threshold = int(cfg.get("crowd_threshold") or self.DEFAULT_THRESHOLD)
        thr_conf  = float(cfg.get("confidence_threshold", 0.5))
        extra = cfg.get("extra") or {}
        dwell = max(0.0, float(
            cfg.get("dwell_time_seconds")
            if cfg.get("dwell_time_seconds") is not None
            else self.DEFAULT_DWELL_SECONDS
        ))
        max_motion = max(0.0, float(
            extra.get("max_motion_norm", self.DEFAULT_MAX_MOTION_NORM)))
        rearm = max(0.0, float(
            extra.get("incident_rearm_seconds", self.DEFAULT_REARM_SECONDS)))
        now = float(ctx.timestamp)

        tracked_people = [
            (tr, det) for tr, det in ctx.tracks
            if tr.cls in COCO_PERSON and float(det.get("conf", 0.0)) >= thr_conf
        ]
        visible_ids = {int(tr.track_id) for tr, _ in tracked_people}
        for key in [key for key in self._stationary_since
                    if key[0] == ctx.camera_id and key[1] not in visible_ids]:
            self._stationary_since.pop(key, None)

        qualified: list[tuple] = []
        for tr, det in tracked_people:
            key = (ctx.camera_id, int(tr.track_id))
            if not self._locally_stationary(tr, max_motion):
                self._stationary_since.pop(key, None)
                continue
            since = self._stationary_since.setdefault(key, now)
            if now - since >= dwell:
                qualified.append((tr, det))

        if len(qualified) < threshold:
            if ctx.camera_id in self._active_tracks:
                clear_since = self._clear_since.setdefault(ctx.camera_id, now)
                if now - clear_since >= rearm:
                    self._active_tracks.pop(ctx.camera_id, None)
                    self._clear_since.pop(ctx.camera_id, None)
            return []

        self._clear_since.pop(ctx.camera_id, None)

        # A staff-only/restricted-area rule is more specific and consequential
        # than a generic crowd rule.  Let StaffZoneDetector/IntrusionDetector
        # own this scene rather than giving operators two conflicting labels.
        priority_zones = [
            z for z in ctx.zones
            if not z.get("suppressed")
            and ({"staff_zone", "staff_area", "restricted"}
                 & set(z.get("detection_types_json") or []))
        ]
        if any(
            bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"])
            for _tr, det in qualified for z in priority_zones
        ):
            return []

        track_ids = {int(tr.track_id) for tr, _ in qualified}
        if ctx.camera_id in self._active_tracks:
            self._active_tracks[ctx.camera_id].update(track_ids)
            return []
        self._active_tracks[ctx.camera_id] = set(track_ids)

        # Use the spatial centroid of qualified people for the thumbnail.
        people = [det for _tr, det in qualified]
        xs = [bbox_centre(p["bbox_norm"])[0] for p in people]
        ys = [bbox_centre(p["bbox_norm"])[1] for p in people]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        return [DetectionEvent(
            detection_type=self.detection_type,
            cls="people_count",
            confidence=min(1.0, len(people) / max(threshold, 1)),
            bbox_norm=[max(0.0, cx - 0.1), max(0.0, cy - 0.1),
                       min(1.0, cx + 0.1), min(1.0, cy + 0.1)],
            track_id=min(track_ids),
            extra={"count": len(people), "threshold": threshold,
                   "dwell_seconds": dwell,
                   "track_ids": sorted(track_ids),
                   "incident_rearm_seconds": rearm,
                   "rule": "sustained_congregation"},
        )]


class OccupancyDetector(Detector):
    detection_type = "occupancy"

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        max_count = int(cfg.get("crowd_threshold") or 0)
        thr_conf  = float(cfg.get("confidence_threshold", 0.5))
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        relevant = [z for z in ctx.zones if "occupancy" in (z.get("detection_types_json") or [])]
        out: list[DetectionEvent] = []
        if not relevant:
            count = len(people)
            if max_count and count >= max_count:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="occupancy",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1],
                    extra={"count": count, "threshold": max_count},
                ))
            return out
        for z in relevant:
            count = sum(1 for p in people if bbox_in_zone(p["bbox_norm"], z["polygon_coords_json"]))
            if max_count and count >= max_count:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="occupancy",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1], zone_id=z["id"],
                    extra={"count": count, "threshold": max_count, "zone": z["name"]},
                ))
        return out


class TrespassDetector(Detector):
    """Anything in a zone whose `detection_types_json` contains 'trespass'."""

    detection_type = "trespass"
    needs_tracking = True

    # P5: a person/vehicle must be tracked inside the zone for this many
    # CONSECUTIVE frames before trespass fires — kills brief walk-through
    # false alerts. Persistent ByteTrack IDs make the per-track counter
    # reliable (IOUTracker fallback also supplies stable ids).
    MIN_ZONE_FRAMES = 5

    def __init__(self):
        # (camera_id, track_id, zone_id) → consecutive frames in zone.
        self._zone_frames: dict[tuple[int, int, int], int] = {}
        # keys that already fired this sustained presence (reset on exit).
        self._fired: set[tuple[int, int, int]] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr = float(cfg.get("confidence_threshold", 0.5))
        zones = [z for z in ctx.zones
                 if "trespass" in (z.get("detection_types_json") or [])
                 and not z.get("suppressed")]
        if not zones:
            return []
        from app.config import settings as _s
        min_frames = int(getattr(_s, "trespass_min_frames", self.MIN_ZONE_FRAMES))
        _wh = None
        if ctx.frame_bgr is not None:
            _h, _w = ctx.frame_bgr.shape[:2]
            _wh = (_w, _h)
        out: list[DetectionEvent] = []
        seen_keys: set[tuple[int, int, int]] = set()
        for tr, det in (ctx.tracks or []):
            if det.get("conf", 0.0) < thr or det["cls"] not in COCO_PERSON | COCO_VEHICLE:
                continue
            for z in zones:
                if not zone_contains(tr.bbox_norm, z["polygon_coords_json"],
                                     zone_id=z["id"], frame_wh=_wh):
                    continue
                key = (ctx.camera_id, tr.track_id, z["id"])
                seen_keys.add(key)
                n = self._zone_frames.get(key, 0) + 1
                self._zone_frames[key] = n
                if n >= min_frames and key not in self._fired:
                    self._fired.add(key)
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls=det["cls"],
                        confidence=det["conf"], bbox_norm=tr.bbox_norm,
                        zone_id=z["id"],
                    ))
                break
        # Reset counters + fired flags for (track,zone) pairs no longer in a zone.
        for key in [k for k in self._zone_frames if k not in seen_keys]:
            self._zone_frames.pop(key, None)
            self._fired.discard(key)
        return out


class LoiteringDetector(Detector):
    """A person tracked inside a loiter-tagged zone for ≥ dwell seconds."""

    detection_type = "loitering"
    needs_tracking = True
    # Track the time a track first entered the zone.
    _zone_enter_time: dict[tuple[int, int], float]   # (camera_id, track_id)

    def __init__(self):
        self._zone_enter_time = {}
        self._fired: set[tuple[int, int, int]] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        dwell = int(cfg.get("dwell_time_seconds") or 30)
        zones = [z for z in ctx.zones if "loitering" in (z.get("detection_types_json") or [])]
        if not zones:
            return []
        out: list[DetectionEvent] = []
        now = time.time()
        for tr, det in ctx.tracks:
            if det["cls"] not in COCO_PERSON:
                continue
            for z in zones:
                if not bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    self._zone_enter_time.pop((ctx.camera_id, tr.track_id), None)
                    continue
                key = (ctx.camera_id, tr.track_id)
                t0 = self._zone_enter_time.get(key, now)
                self._zone_enter_time.setdefault(key, now)
                if now - t0 >= dwell:
                    fkey = (ctx.camera_id, tr.track_id, z["id"])
                    if fkey in self._fired:
                        continue
                    self._fired.add(fkey)
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls="person",
                        confidence=det["conf"], bbox_norm=tr.bbox_norm,
                        track_id=tr.track_id, zone_id=z["id"],
                        extra={"dwell_seconds": int(now - t0)},
                    ))
        return out


class AbandonedObjectDetector(Detector):
    """An object track that is *not* a person/animal and remains stationary
    (low IOU drift) for ≥ N seconds."""

    detection_type = "abandoned_object"
    needs_tracking = True

    def __init__(self):
        self._first_seen: dict[int, float] = {}
        self._first_box:  dict[int, list[float]] = {}
        self._fired: set[int] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        dwell = int(cfg.get("dwell_time_seconds") or 60)
        out: list[DetectionEvent] = []
        now = time.time()
        for tr, det in ctx.tracks:
            if det["cls"] in COCO_PERSON or det["cls"] in {"cat", "dog", "bird"}:
                continue
            self._first_seen.setdefault(tr.track_id, now)
            self._first_box.setdefault(tr.track_id, list(tr.bbox_norm))
            drift = 1.0 - iou(self._first_box[tr.track_id], tr.bbox_norm)
            if drift > 0.4:
                # Object moved too much to be considered "abandoned".
                self._first_seen[tr.track_id] = now
                self._first_box[tr.track_id]  = list(tr.bbox_norm)
                continue
            if (now - self._first_seen[tr.track_id]) >= dwell and tr.track_id not in self._fired:
                self._fired.add(tr.track_id)
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls=det["cls"],
                    confidence=det["conf"], bbox_norm=tr.bbox_norm,
                    track_id=tr.track_id,
                    extra={"dwell_seconds": int(now - self._first_seen[tr.track_id])},
                ))
        return out


class TripwireDetector(Detector):
    """A track's centre crosses a `shape="line"` zone."""

    detection_type = "tripwire"
    needs_tracking = True

    def __init__(self):
        # Last known centre per (camera_id, track_id, zone_id).
        self._last: dict[tuple[int, int, int], tuple[float, float]] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        lines = [z for z in ctx.zones if z.get("shape") == "line"
                 and "tripwire" in (z.get("detection_types_json") or [])]
        if not lines:
            return []
        out: list[DetectionEvent] = []
        for tr, _ in ctx.tracks:
            cx, cy = bbox_centre(tr.bbox_norm)
            for z in lines:
                pts = z["polygon_coords_json"]
                if len(pts) < 2:
                    continue
                p3, p4 = tuple(pts[0]), tuple(pts[1])
                key = (ctx.camera_id, tr.track_id, z["id"])
                prev = self._last.get(key)
                self._last[key] = (cx, cy)
                if prev is None:
                    continue
                if segments_cross(prev, (cx, cy), p3, p4):
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls=tr.cls,
                        confidence=1.0, bbox_norm=tr.bbox_norm,
                        track_id=tr.track_id, zone_id=z["id"],
                    ))
        return out


class TailgatingDetector(Detector):
    """Two persons cross the same tripwire within `dwell_time_seconds`."""

    detection_type = "tailgating"
    needs_tracking = True

    def __init__(self):
        self._tripwire = TripwireDetector()
        self._recent_crosses: dict[int, list[float]] = {}    # zone_id -> [timestamps]

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        # Reuse tripwire crossings from the same frame.
        # Synthesize a tripwire config so the inner detector runs.
        ctx.config.setdefault("tripwire", {"enabled": True, "confidence_threshold": 0.5})
        crossings = self._tripwire.evaluate(ctx)
        out: list[DetectionEvent] = []
        window = int(cfg.get("dwell_time_seconds") or 3)
        now = time.time()
        for ev in crossings:
            if ev.cls not in COCO_PERSON:
                continue
            zid = ev.zone_id or 0
            arr = self._recent_crosses.setdefault(zid, [])
            arr.append(now)
            # Trim window.
            self._recent_crosses[zid] = [t for t in arr if now - t <= window]
            if len(self._recent_crosses[zid]) >= 2:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="person",
                    confidence=1.0, bbox_norm=ev.bbox_norm,
                    track_id=ev.track_id, zone_id=zid,
                    extra={"window_seconds": window,
                           "count_in_window": len(self._recent_crosses[zid])},
                ))
                self._recent_crosses[zid] = []
        return out


class FallDetector(Detector):
    """Heuristic: a person bbox whose width > 1.4× height is likely fallen.
    Real fall detection wants a pose model (YOLOv8-pose) — left as upgrade."""

    detection_type = "fall"

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr = float(cfg.get("confidence_threshold", 0.5))
        out: list[DetectionEvent] = []
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON or det["conf"] < thr:
                continue
            x1, y1, x2, y2 = det["bbox_norm"]
            w, h = (x2 - x1), (y2 - y1)
            if h <= 0:
                continue
            aspect = w / h
            if aspect >= 1.4:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="person",
                    confidence=det["conf"] * min(1.0, aspect / 2.0),
                    bbox_norm=det["bbox_norm"],
                    extra={"aspect_ratio": round(aspect, 2)},
                ))
        return out


class HeatmapDetector(Detector):
    """Accumulates per-window density grids; no events emitted.

    Keeps THREE traffic grids per camera per window plus three
    behavioural layers — engagement, congestion, and paths — so the
    UI can show "where do customers genuinely linger" separately from
    "where do people merely pass through".

    Layers (each per camera per window):

      traffic     — frame-count of people in each cell. Premier-League
                    style hotspots. Includes every zone, every track.
      engagement  — dwell-seconds accumulated by NON-STAFF tracks
                    inside aisle / shelf / window / display zones,
                    counting only cells the track sat in for ≥3s
                    (transit movements are ignored). Repeat-visits to
                    a zone are weighted ×1.5.
      congestion  — same accumulator but for queue / counter zones.
                    Separated so checkout backlogs don't masquerade
                    as merchandising interest.
      paths       — edge-counts between consecutive cells a track
                    visits. Drives the customer-flow arrows.

    Redis keys (per camera per window in ('3h', 'day', 'week')):

      vg:heatmap:{window}:{camera_id}             traffic (legacy name)
      vg:heatmap:engagement:{window}:{camera_id}  engagement layer
      vg:heatmap:congestion:{window}:{camera_id}  congestion layer
      vg:heatmap:paths:{window}:{camera_id}       movement edges

    `vg:heatmap:{camera_id}` (un-suffixed) remains populated with the
    DAY traffic grid so older call sites still work.
    """

    detection_type = "heatmap"
    GRID = 20
    # Publish every 10s so a person who appears on camera shows up on
    # the heatmap within ~10s — operators were watching empty grids
    # while their store was visibly busy because the old 30s cadence
    # was too slow for the "is this thing on?" check.
    PUBLISH_INTERVAL = 10
    WINDOWS = ("3h", "day", "week")

    # Engagement vs congestion zone tags. A zone whose tags include
    # any ENGAGEMENT_TAG counts dwell into the engagement layer; a
    # zone whose tags include any CONGESTION_TAG counts into the
    # congestion layer. A zone with neither contributes only traffic.
    ENGAGEMENT_TAGS = {"aisle", "shelf", "window", "display", "dwell"}
    CONGESTION_TAGS = {"queue", "counter"}

    # A track must sit in a cell for at least this many seconds before
    # the dwell counts — pass-throughs are filtered out.
    MIN_DWELL_SECONDS = 3.0

    def __init__(self):
        # camera_id -> {window_name -> grid}
        self.grids: dict[int, dict[str, list[list[int]]]] = {}
        # New behavioural layers — same shape as `grids` but floats
        # (accumulated dwell seconds, possibly with ×1.5 repeat-visit
        # weighting).
        self.engagement: dict[int, dict[str, list[list[float]]]] = {}
        self.congestion: dict[int, dict[str, list[list[float]]]] = {}
        # Movement edges. Key: (gy_from, gx_from, gy_to, gx_to) → count.
        self.paths: dict[int, dict[str, dict[tuple, int]]] = {}
        # camera_id -> {window_name -> period_anchor}
        self.anchors: dict[int, dict[str, tuple]] = {}
        self._last_publish: dict[int, float] = {}

        # Per-track session state — keyed by (camera_id, track_id).
        # Tracks the cell + zone the track is currently sitting in,
        # when it arrived, and which zones the session has already
        # visited (for repeat-visit weighting).
        self._sessions: dict[tuple[int, int], dict] = {}

    @staticmethod
    def _period_anchor(window: str, ts: datetime | None = None) -> tuple:
        """Tuple uniquely identifying the current window-period.

        Comparable across calls — if the anchor changed since last
        read, the grid is stale and must be reset.

        3h   : (date, hour_bucket)  where hour_bucket = hour // 3 * 3,
               so values are always one of {0, 3, 6, 9, 12, 15, 18, 21}
        day  : (date,)
        week : (iso_year, iso_week)
        """
        from datetime import datetime
        ts = ts or datetime.now()
        if window == "3h":
            return (ts.date(), (ts.hour // 3) * 3)
        if window == "week":
            iso = ts.isocalendar()
            # (iso_year, iso_week)
            return (iso[0], iso[1])
        # default: day
        return (ts.date(),)

    def _ensure_grid(self, camera_id: int, window: str) -> list[list[int]]:
        """Return the traffic grid for (camera, window), resetting all
        four layers if the anchor has advanced."""
        from datetime import datetime
        cam_grids       = self.grids.setdefault(camera_id, {})
        cam_engagement  = self.engagement.setdefault(camera_id, {})
        cam_congestion  = self.congestion.setdefault(camera_id, {})
        cam_paths       = self.paths.setdefault(camera_id, {})
        cam_anchors     = self.anchors.setdefault(camera_id, {})
        anchor = self._period_anchor(window, datetime.now())
        if cam_anchors.get(window) != anchor:
            cam_grids[window]      = [[0]   * self.GRID for _ in range(self.GRID)]
            cam_engagement[window] = [[0.0] * self.GRID for _ in range(self.GRID)]
            cam_congestion[window] = [[0.0] * self.GRID for _ in range(self.GRID)]
            cam_paths[window]      = {}
            cam_anchors[window]    = anchor
        return cam_grids[window]

    def _publish(self, camera_id: int) -> None:
        try:
            import json
            import time as _t
            import redis
            from app.config import settings
            r = redis.from_url(settings.redis_url)
            now_ts = _t.time()
            cam_grids       = self.grids.get(camera_id, {})
            cam_engagement  = self.engagement.get(camera_id, {})
            cam_congestion  = self.congestion.get(camera_id, {})
            cam_paths       = self.paths.get(camera_id, {})
            for window in self.WINDOWS:
                grid = cam_grids.get(window) or []
                payload = {
                    "grid": grid, "size": self.GRID,
                    "window": window,
                    "updated_at": now_ts,
                }
                r.set(f"vg:heatmap:{window}:{camera_id}",
                      json.dumps(payload), ex=24 * 3600)

                eng = cam_engagement.get(window) or []
                r.set(f"vg:heatmap:engagement:{window}:{camera_id}",
                      json.dumps({"grid": eng, "size": self.GRID,
                                  "window": window, "updated_at": now_ts}),
                      ex=24 * 3600)
                con = cam_congestion.get(window) or []
                r.set(f"vg:heatmap:congestion:{window}:{camera_id}",
                      json.dumps({"grid": con, "size": self.GRID,
                                  "window": window, "updated_at": now_ts}),
                      ex=24 * 3600)
                edges = cam_paths.get(window) or {}
                edge_list = [
                    {"from": [k[0], k[1]], "to": [k[2], k[3]], "count": v}
                    for k, v in edges.items() if v > 0
                ]
                edge_list.sort(key=lambda e: -e["count"])
                r.set(f"vg:heatmap:paths:{window}:{camera_id}",
                      json.dumps({"edges": edge_list[:200], "size": self.GRID,
                                  "window": window, "updated_at": now_ts}),
                      ex=24 * 3600)

            # Backwards-compat: the legacy un-suffixed key holds the
            # DAY traffic grid. Pre-time-window callers keep working.
            day_grid = cam_grids.get("day") or []
            r.set(
                f"vg:heatmap:{camera_id}",
                json.dumps({"grid": day_grid, "size": self.GRID,
                            "updated_at": now_ts}),
                ex=24 * 3600,
            )
        except Exception:
            pass

    @staticmethod
    def _track_signature(camera_id: int, track_id: int) -> str:
        """Stable per-day track key used to LEFT-JOIN against
        staff_tracks for the staff exclusion. Mirrors the format the
        UniqueVisitor / Journey detectors use."""
        return f"{camera_id}:{track_id}"

    def _is_staff(self, ctx: DetectorContext, track_id: int) -> bool:
        """True if the track's signature is in today's staff_tracks
        roster. Best-effort — returns False on any DB hiccup so we
        never silently drop customer data. Importantly rolls back on
        failure so we don't poison the transaction for the rest of
        the detector chain (InFailedSqlTransaction)."""
        if ctx.db is None or ctx.store_id is None:
            return False
        try:
            from app.models.staff import StaffTrack
            from datetime import datetime as _dt
            sig = self._track_signature(ctx.camera_id, track_id)
            row = (ctx.db.query(StaffTrack)
                       .filter(StaffTrack.store_id == ctx.store_id,
                               StaffTrack.day == _dt.utcnow().date(),
                               StaffTrack.track_signature == sig)
                       .first())
            return bool(row and row.classified_as == "staff")
        except Exception:
            try:
                ctx.db.rollback()
            except Exception:
                pass
            return False

    @staticmethod
    def _zone_tags_at(ctx: DetectorContext, bbox_norm) -> set[str]:
        """Union of detection_types_json across every zone that
        contains the given bbox centroid. Used to decide whether dwell
        feeds the engagement or congestion layer."""
        tags: set[str] = set()
        for z in ctx.zones:
            if bbox_in_zone(bbox_norm, z.get("polygon_coords_json") or []):
                tags.update(z.get("detection_types_json") or [])
        return tags

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        grids = [self._ensure_grid(ctx.camera_id, w) for w in self.WINDOWS]
        import time as _t
        now_ts = _t.time()

        # Decide which detection classes count as "activity" for the
        # heatmap. Default-COCO cameras give us "person" directly. But
        # cameras assigned a custom AI model (weapons, fire, custom
        # retail SKUs) won't emit "person" at all — so the heatmap
        # would stay forever empty on those cameras. Fix: if NONE of
        # this frame's detections are persons but the frame DID
        # produce detections, treat every detection as activity. The
        # heatmap is about "where things are happening" — for retail
        # cameras the only real moving subjects are customers, so
        # this is safe in practice.
        person_dets = [d for d in ctx.raw_detections
                       if d.get("cls") in COCO_PERSON]
        activity_dets = person_dets or ctx.raw_detections

        # Traffic layer — per-cell frame count, every active subject.
        for det in activity_dets:
            cx, cy = bbox_centre(det["bbox_norm"])
            gx = min(self.GRID - 1, max(0, int(cx * self.GRID)))
            gy = min(self.GRID - 1, max(0, int(cy * self.GRID)))
            for g in grids:
                g[gy][gx] += 1

        # Engagement / congestion / paths — track-aware. We credit
        # dwell every frame for active tracks (not just on cell
        # transitions) so the engagement layer fills immediately when
        # a customer is standing still. Earlier behaviour only flushed
        # on cell change, which left stationary customers invisible.
        active_track_ids: set[int] = set()
        currently_active_cells: set[tuple[int, int]] = set()
        # Defence in depth — record every cell with activity in the
        # current FRAME from raw_detections too, not just from tracks.
        # If the IOU tracker dropped a frame (low FPS, occlusion) we
        # still want the live-activity boost to light up the cell so
        # the operator sees the heatmap respond.
        for det in activity_dets:
            cx, cy = bbox_centre(det["bbox_norm"])
            gx = min(self.GRID - 1, max(0, int(cx * self.GRID)))
            gy = min(self.GRID - 1, max(0, int(cy * self.GRID)))
            currently_active_cells.add((gy, gx))
        # Build a quick lookup of activity-class names so the per-
        # track loop can mirror the same "person OR fallback-to-all"
        # rule the traffic loop just used.
        activity_classes = {d.get("cls") for d in activity_dets}
        for tr, _det in ctx.tracks:
            if tr.cls not in activity_classes:
                continue
            active_track_ids.add(tr.track_id)
            cx, cy = bbox_centre(tr.bbox_norm)
            gx = min(self.GRID - 1, max(0, int(cx * self.GRID)))
            gy = min(self.GRID - 1, max(0, int(cy * self.GRID)))
            currently_active_cells.add((gy, gx))
            sess_key = (ctx.camera_id, tr.track_id)
            sess = self._sessions.get(sess_key)
            if sess is None:
                self._sessions[sess_key] = {
                    "cell": (gy, gx),
                    "cell_entered_at": now_ts,
                    "last_frame_ts": now_ts,
                    "zone_tags": self._zone_tags_at(ctx, tr.bbox_norm),
                    "visited_zones": set(),
                    "last_cell": (gy, gx),
                    "is_staff": self._is_staff(ctx, tr.track_id),
                }
                continue   # first frame: no dwell delta to credit yet
            if sess["cell"] != (gy, gx):
                # Track moved to a new cell. Flush leftover dwell + record
                # the path edge, then start a fresh cell session.
                self._flush_cell_dwell(ctx.camera_id, sess, now_ts)
                from_cell = sess["last_cell"]
                if from_cell != (gy, gx):
                    for w in self.WINDOWS:
                        edges = self.paths[ctx.camera_id].setdefault(w, {})
                        ek = (from_cell[0], from_cell[1], gy, gx)
                        edges[ek] = edges.get(ek, 0) + 1
                sess["last_cell"] = (gy, gx)
                sess["cell"] = (gy, gx)
                sess["cell_entered_at"] = now_ts
                sess["zone_tags"] = self._zone_tags_at(ctx, tr.bbox_norm)
            else:
                # Same cell — credit the per-frame delta directly into
                # the engagement / congestion layer so a stationary
                # customer still produces heat. Still gated by the 3s
                # threshold so pass-throughs aren't promoted to dwell.
                if not sess.get("is_staff"):
                    dwell_so_far = now_ts - sess["cell_entered_at"]
                    if dwell_so_far >= self.MIN_DWELL_SECONDS:
                        delta = max(0.0, now_ts - sess["last_frame_ts"])
                        self._add_dwell_credit(ctx.camera_id, gy, gx,
                                               sess["zone_tags"], delta)
            sess["last_frame_ts"] = now_ts

        # Sessions whose track aged out — flush leftover dwell so a
        # customer who left the frame mid-look still gets credit.
        for sess_key in list(self._sessions.keys()):
            cam_id, tid = sess_key
            if cam_id != ctx.camera_id:
                continue
            if tid not in active_track_ids:
                self._flush_cell_dwell(cam_id, self._sessions[sess_key], now_ts)
                self._sessions.pop(sess_key, None)

        # Minimum-visibility boost: any cell with a person RIGHT NOW
        # gets at least 30% of the grid max in the engagement layer,
        # and adjacent cells get 15%. This way the operator sees the
        # heatmap respond instantly to people on screen instead of
        # staring at an empty grid waiting for the 3-second dwell.
        self._stamp_live_activity(ctx.camera_id, currently_active_cells)

        if now_ts - self._last_publish.get(ctx.camera_id, 0) >= self.PUBLISH_INTERVAL:
            self._last_publish[ctx.camera_id] = now_ts
            day_grid = self.grids.get(ctx.camera_id, {}).get("day") or []
            active = sum(1 for row in day_grid for v in row if v > 0)
            import logging as _l
            # Diagnostic spread across each stage of the pipeline so
            # we can immediately see whether a "0 cells active" result
            # is a YOLO miss, a tracker drop, or a class-name mismatch.
            _l.getLogger(__name__).info(
                "Heatmap cam=%d  raw=%d persons=%d activity=%d "
                "tracks=%d cells_active=%d",
                ctx.camera_id, len(ctx.raw_detections), len(person_dets),
                len(activity_dets), len(active_track_ids), active,
            )
            self._publish(ctx.camera_id)
        return []     # heatmap doesn't generate alerts

    def _add_dwell_credit(self, camera_id: int, gy: int, gx: int,
                          tags: set[str], delta: float) -> None:
        """Add `delta` dwell-seconds into the matching layer(s) for
        cell (gy, gx). When no zone tags are present, default to the
        engagement layer so unconfigured cameras still light up — the
        operator workflow rarely involves zoning every camera before
        the heatmap is expected to work."""
        if delta <= 0:
            return
        engagement_match = bool(tags & self.ENGAGEMENT_TAGS) or not tags
        congestion_match = bool(tags & self.CONGESTION_TAGS)
        for w in self.WINDOWS:
            if engagement_match:
                grid = self.engagement[camera_id].get(w)
                if grid:
                    grid[gy][gx] += delta
            if congestion_match:
                grid = self.congestion[camera_id].get(w)
                if grid:
                    grid[gy][gx] += delta

    def _stamp_live_activity(self, camera_id: int,
                              cells: set[tuple[int, int]]) -> None:
        """Boost cells with a person currently in frame so the heatmap
        always shows SOMETHING when the operator can see customers.

        Current cell -> max(value, current_max * 0.3) -> green/active
        8-neighbours -> max(value, current_max * 0.15) -> faint blue
        """
        if not cells:
            return
        for w in self.WINDOWS:
            grid = self.engagement.get(camera_id, {}).get(w)
            if not grid:
                continue
            # Floor of 1.0 means a never-touched grid still gets some
            # baseline so the boost actually shows colour.
            current_max = max((max(row) for row in grid), default=0) or 1.0
            for (gy, gx) in cells:
                grid[gy][gx] = max(grid[gy][gx], current_max * 0.3)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0:
                            continue
                        ny, nx = gy + dy, gx + dx
                        if 0 <= ny < self.GRID and 0 <= nx < self.GRID:
                            grid[ny][nx] = max(grid[ny][nx],
                                                current_max * 0.15)

    def _flush_cell_dwell(self, camera_id: int, sess: dict, now_ts: float) -> None:
        """Credit a track's accumulated dwell in a cell into the
        engagement / congestion layer when the track moves away. Pass-
        throughs (<3s) and staff are filtered. Repeat visits to the
        same zone tag-set within the session weight ×1.5."""
        if sess.get("is_staff"):
            return
        dwell = now_ts - sess.get("cell_entered_at", now_ts)
        if dwell < self.MIN_DWELL_SECONDS:
            return
        gy, gx = sess["cell"]
        tags = sess["zone_tags"]
        engagement_match = bool(tags & self.ENGAGEMENT_TAGS)
        congestion_match = bool(tags & self.CONGESTION_TAGS)

        zone_key = tuple(sorted(tags))
        weight = 1.5 if zone_key in sess["visited_zones"] else 1.0
        sess["visited_zones"].add(zone_key)
        credit = dwell * weight

        for w in self.WINDOWS:
            if engagement_match:
                grid = self.engagement[camera_id].get(w)
                if grid:
                    grid[gy][gx] += credit
            if congestion_match:
                grid = self.congestion[camera_id].get(w)
                if grid:
                    grid[gy][gx] += credit


class LPRDetector(Detector):
    """License plate placeholder. Pipeline is: vehicle bbox → crop → OCR.

    Real OCR (PaddleOCR / EasyOCR) is heavy; we ship a stub that emits an
    event with an empty plate string when a vehicle is sustained in view.
    Operators can plug in their preferred OCR by overriding `recognise`.
    """

    detection_type = "lpr"
    needs_tracking = True

    def __init__(self):
        self._fired: set[int] = set()

    _reader = None    # cached EasyOCR reader (lazy-loaded on first call)

    def recognise(self, frame_bgr, bbox_px) -> str:
        """Crop the vehicle bbox and run OCR. Returns the highest-confidence
        plate-shaped string (>= 4 chars, mostly A-Z0-9), or '' if none.

        Uses EasyOCR if installed (`pip install easyocr`). If not present,
        returns '' silently — the event still fires with the bbox so
        operators can wire their preferred OCR later by subclassing.
        """
        if frame_bgr is None or bbox_px is None:
            return ""
        try:
            if LPRDetector._reader is None:
                import easyocr
                LPRDetector._reader = easyocr.Reader(["en"], gpu=False)
            x1, y1, x2, y2 = [int(v) for v in bbox_px]
            x1, y1 = max(0, x1), max(0, y1)
            crop = frame_bgr[y1:y2, x1:x2]
            if crop.size == 0:
                return ""
            results = LPRDetector._reader.readtext(crop, detail=1, paragraph=False)
            # Pick the highest-confidence string that looks plate-ish.
            best = ""
            best_conf = 0.0
            for _box, text, conf in results:
                cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
                if len(cleaned) >= 4 and conf > best_conf:
                    best, best_conf = cleaned, conf
            return best
        except ImportError:
            return ""
        except Exception:
            return ""

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr = float(cfg.get("confidence_threshold", 0.6))
        out: list[DetectionEvent] = []
        for tr, det in ctx.tracks:
            if det["cls"] not in COCO_VEHICLE or det["conf"] < thr:
                continue
            if tr.track_id in self._fired:
                continue
            self._fired.add(tr.track_id)
            plate = ""
            provider = "stub"
            # Try OCR — frame_bgr is set by the inference worker.
            try:
                plate = self.recognise(ctx.frame_bgr, det.get("bbox_px"))
                provider = "easyocr" if plate else "stub"
            except Exception:
                pass
            out.append(DetectionEvent(
                detection_type=self.detection_type, cls=det["cls"],
                confidence=det["conf"], bbox_norm=det["bbox_norm"],
                track_id=tr.track_id,
                extra={"plate": plate, "ocr_provider": provider,
                       "store_id": ctx.store_id},
            ))
        return out
