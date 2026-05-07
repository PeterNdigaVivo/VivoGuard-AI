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

from app.ai.detectors.base import COCO_PERSON, COCO_VEHICLE, Detector, DetectorContext, DetectionEvent
from app.ai.zone_logic import bbox_centre, bbox_in_zone, iou, segments_cross


class CrowdDetector(Detector):
    detection_type = "crowd"

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        threshold = int(cfg.get("crowd_threshold") or 5)
        thr_conf  = float(cfg.get("confidence_threshold", 0.5))
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        if len(people) < threshold:
            return []
        # Use the spatial centroid of all people for the alert thumbnail.
        xs = [bbox_centre(p["bbox_norm"])[0] for p in people]
        ys = [bbox_centre(p["bbox_norm"])[1] for p in people]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        return [DetectionEvent(
            detection_type=self.detection_type,
            cls="people_count",
            confidence=min(1.0, len(people) / max(threshold, 1)),
            bbox_norm=[max(0.0, cx - 0.1), max(0.0, cy - 0.1),
                       min(1.0, cx + 0.1), min(1.0, cy + 0.1)],
            extra={"count": len(people), "threshold": threshold},
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
        out: list[DetectionEvent] = []
        for det in ctx.raw_detections:
            if det["conf"] < thr or det["cls"] not in COCO_PERSON | COCO_VEHICLE:
                continue
            for z in zones:
                if bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"]):
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls=det["cls"],
                        confidence=det["conf"], bbox_norm=det["bbox_norm"],
                        zone_id=z["id"],
                    ))
                    break
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
    """Accumulates a coarse density grid; no events emitted (read via API)."""

    detection_type = "heatmap"
    GRID = 32

    def __init__(self):
        self.grid: dict[int, list[list[int]]] = {}     # camera_id -> grid

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        g = self.grid.setdefault(ctx.camera_id, [[0] * self.GRID for _ in range(self.GRID)])
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON:
                continue
            cx, cy = bbox_centre(det["bbox_norm"])
            gx = min(self.GRID - 1, max(0, int(cx * self.GRID)))
            gy = min(self.GRID - 1, max(0, int(cy * self.GRID)))
            g[gy][gx] += 1
        return []     # heatmap doesn't generate alerts


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

    def recognise(self, frame, bbox_px) -> str:                 # noqa: ARG002
        return ""    # plug your OCR here

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
            out.append(DetectionEvent(
                detection_type=self.detection_type, cls=det["cls"],
                confidence=det["conf"], bbox_norm=det["bbox_norm"],
                track_id=tr.track_id,
                extra={"plate": "", "ocr_provider": "stub"},
            ))
        return out
