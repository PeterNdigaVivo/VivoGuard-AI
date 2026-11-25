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
from app.ai.calibration import effective_threshold


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
        out: list[DetectionEvent] = []
        # Constrain to relevant zones (if any zones list this detection type).
        relevant_zones = [z for z in ctx.zones
                          if (self.detection_type in (z.get("detection_types_json") or []))
                          and not z.get("suppressed")]
        for det in ctx.raw_detections:
            if det["cls"] not in self.class_set:
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
            thr = effective_threshold(cfg, zone_id=zone_id, default=0.6)
            score = float(det.get("rolling_conf", det["conf"]))
            if score < thr:
                continue
            out.append(DetectionEvent(
                detection_type=self.detection_type,
                cls=det["cls"], confidence=score,
                bbox_norm=det["bbox_norm"], zone_id=zone_id,
                # The inference worker's temporal gate is track-based.  The
                # tracker attaches this id to the original detection dict;
                # dropping it here allowed a one-frame classifier glitch to
                # create an operator alert immediately.
                track_id=det.get("track_id"),
                extra=self.extra_from_det(det),
            ))
        return out

    def extra_from_det(self, det: dict) -> dict:                # noqa: ARG002
        return {}


class PersonDetector(_ClassFilterDetector):
    """person alerts were the platform's biggest false-alarm source:
    with no person-tagged zone the base class fired on EVERY person
    anywhere in frame, all day, staff and customers included. Three
    gates (Nov-2026):

      zone requirement  — a camera with no person-tagged zone raises
                          no person alerts (operator draws the zone
                          that matters: a stockroom door, a safe, a
                          fire exit). Per-camera escape hatch:
                          config extra {"allow_zoneless": true}.
                          Global kill switch:
                          settings.person_requires_zone = False.
      staff exclusion   — detections inside a pure staff zone, or
                          classified high/medium staff by
                          staff_identity, don't alert. Per-camera
                          escape hatch: extra {"include_staff": true}.
                          Global: settings.person_exclude_staff.
      trading hours     — OPT-IN per camera: extra
                          {"only_when_closed": true} suppresses while
                          the store is open (overnight coverage of
                          the whole frame is IntrusionDetector's job;
                          this is for door cameras that only care
                          about people when the store is shut).
    """

    detection_type = "person"
    class_set = COCO_PERSON

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        from app.config import settings

        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        extra = cfg.get("extra") or {}

        has_zone = any(
            self.detection_type in (z.get("detection_types_json") or [])
            and not z.get("suppressed")
            for z in ctx.zones)
        if (not has_zone
                and bool(getattr(settings, "person_requires_zone", True))
                and not extra.get("allow_zoneless")):
            return []

        if (extra.get("only_when_closed")
                and ctx.store_id is not None and ctx.store_timezone):
            try:
                from app.utils.business_hours import (
                    is_open_with_default, localised_now)
                if is_open_with_default(ctx.business_hours,
                                        localised_now(ctx.store_timezone)):
                    return []
            except Exception:
                # Unparseable hours must never kill the detector.
                pass

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
        wanted = set((cfg.get("extra") or {}).get("classes") or [])
        out: list[DetectionEvent] = []
        for det in ctx.raw_detections:
            score = float(det.get("rolling_conf", det["conf"]))
            if score < effective_threshold(cfg, default=0.5):
                continue
            if wanted and det["cls"] not in wanted:
                continue
            out.append(DetectionEvent(
                detection_type=self.detection_type,
                cls=det["cls"], confidence=score,
                bbox_norm=det["bbox_norm"],
                track_id=det.get("track_id"),
            ))
        return out
