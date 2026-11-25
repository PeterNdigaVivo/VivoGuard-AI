"""Stateless detectors that only need to filter YOLO output by class.

person, vehicle, animal, fire, smoke, weapon, weapon_brandished, face,
shelf, custom — all inherit this pattern.
"""
from __future__ import annotations

from app.ai.detectors.base import (
    COCO_ANIMAL, COCO_PERSON, COCO_VEHICLE,
    Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone


class _ClassFilterDetector(Detector):
    """Common implementation for class-filter detectors.

    Subclasses override `detection_type` and `class_set` (and optionally
    `extra_from_det`).
    """

    class_set: set[str] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr = float(cfg.get("confidence_threshold", 0.6))
        out: list[DetectionEvent] = []
        # Constrain to relevant zones (if any zones list this detection type).
        relevant_zones = [z for z in ctx.zones
                          if (self.detection_type in (z.get("detection_types_json") or []))
                          and not z.get("suppressed")]
        for det in ctx.raw_detections:
            if det["cls"] not in self.class_set:
                continue
            if det["conf"] < thr:
                continue
            zone_id = None
            if relevant_zones:
                in_any = False
                for z in relevant_zones:
                    if bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"]):
                        in_any = True
                        zone_id = z["id"]
                        break
                if not in_any:
                    continue
            out.append(DetectionEvent(
                detection_type=self.detection_type,
                cls=det["cls"], confidence=det["conf"],
                bbox_norm=det["bbox_norm"], zone_id=zone_id,
                extra=self.extra_from_det(det),
            ))
        return out

    def extra_from_det(self, det: dict) -> dict:                # noqa: ARG002
        return {}


class PersonDetector(_ClassFilterDetector):
    """person alerts carry one extra gate on top of the base class:
    staff exclusion. Detections inside a pure staff zone, or whose
    track the uniform classifier rates high/medium staff, don't
    alert — staff walking through a person-tagged stockroom zone was
    a major false-alarm source. Per-camera escape hatch: config extra
    {"include_staff": true}. Global: settings.person_exclude_staff.

    Deliberately NOT here: zone requirements or trading-hours gates.
    The inference worker's _person_alert_warranted() already
    suppresses daytime person noise (trading hours -> only
    restricted-type zones alert) and it FAILS OPEN after hours — a
    detector-level zone gate would silently drop a 2 a.m. break-in
    on a camera nobody drew zones for, which that design forbids.
    """

    detection_type = "person"
    class_set = COCO_PERSON

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        from app.config import settings

        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        extra = cfg.get("extra") or {}

        events = super().evaluate(ctx)
        if not events:
            return events

        if (not bool(getattr(settings, "person_exclude_staff", True))
                or extra.get("include_staff")):
            return events

        import time as _time
        from app.ai.detectors import staff_identity
        now = _time.time()
        kept: list[DetectionEvent] = []
        for ev in events:
            in_staff, _tag = staff_identity.is_in_pure_staff_zone(
                ctx, ev.bbox_norm)
            if in_staff:
                continue
            det = {"bbox_norm": ev.bbox_norm, "conf": ev.confidence}
            tid = (ev.track_id if ev.track_id is not None
                   else staff_identity.match_track(ctx, det))
            # classify() is "uncertain" when pixels are unreadable —
            # that still alerts: a customer at a stockroom door matters
            # even when their clothing can't be read. Only confident
            # staff verdicts are excluded.
            verdict = staff_identity.classify(ctx, det, tid, now)
            if verdict["level"] in ("high", "medium"):
                continue
            # Context for the alert: whether the uniform classifier read
            # this person as a customer ("unknown") or simply couldn't
            # tell ("uncertain") — so operators see classified pings,
            # not raw detections. Alert wording/UI reads extra.
            ev.extra = {**(ev.extra or {}),
                        "staff_level": verdict["level"],
                        "person_context": ("customer"
                                           if verdict["level"] == "unknown"
                                           else "unidentified")}
            kept.append(ev)
        return kept


class VehicleDetector(_ClassFilterDetector):
    detection_type = "vehicle"
    class_set = COCO_VEHICLE


class AnimalDetector(_ClassFilterDetector):
    detection_type = "animal"
    class_set = COCO_ANIMAL


class FaceDetector(_ClassFilterDetector):
    """Requires a face-trained model (default YOLOv8 doesn't ship faces)."""
    detection_type = "face"
    class_set = {"face"}


class WeaponDetector(_ClassFilterDetector):
    """Requires a weapon-trained custom model (gun, knife, ...)."""
    detection_type = "weapon"
    class_set = {"gun", "weapon", "knife", "rifle", "pistol"}


class WeaponBrandishedDetector(_ClassFilterDetector):
    """A separate label trained for *drawn / aimed* weapons."""
    detection_type = "weapon_brandished"
    class_set = {"gun_brandished", "weapon_drawn"}


class FireDetector(_ClassFilterDetector):
    detection_type = "fire"
    class_set = {"fire", "flame"}


class SmokeDetector(_ClassFilterDetector):
    detection_type = "smoke"
    class_set = {"smoke"}


class ShelfDetector(_ClassFilterDetector):
    """Retail shelf monitoring — assumes a custom 'shelf_empty' class."""
    detection_type = "shelf"
    class_set = {"shelf_empty", "out_of_stock"}


class CustomDetector(_ClassFilterDetector):
    """Wildcard detector for user-trained models. The configured `extra`
    field can specify which class names to fire on; default is all."""

    detection_type = "custom"

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr = float(cfg.get("confidence_threshold", 0.5))
        wanted = set((cfg.get("extra") or {}).get("classes") or [])
        out: list[DetectionEvent] = []
        for det in ctx.raw_detections:
            if det["conf"] < thr:
                continue
            if wanted and det["cls"] not in wanted:
                continue
            out.append(DetectionEvent(
                detection_type=self.detection_type,
                cls=det["cls"], confidence=det["conf"],
                bbox_norm=det["bbox_norm"],
            ))
        return out
