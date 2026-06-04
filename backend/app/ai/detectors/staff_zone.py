"""StaffZoneDetector — behind-counter (staff-only) area compliance.

A camera operator tags a polygon with `staff_zone` to mark the space
BEHIND the service counter where only uniformed staff should be.
This is distinct from the customer-facing `counter` zone the
QueueDetector and StaffPresenceDetector watch.

Logic is now centralised in app.ai.detectors.staff_identity which
classifies each person as high / medium / unknown staff confidence
based on uniform colour + lanyard + time-in-zone. Alerts:

  HIGH or MEDIUM staff → suppress ALL alerts. The person is recorded
                         in staff_tracks so visitor counts exclude
                         them. After-hours intrusion alerts skip
                         them too (rule 3 — "Staff opening / closing
                         store" rather than "Intrusion").

  Correct uniform but no lanyard sustained 5 min  →  INFO
                         "Staff member missing name tag"

  UNKNOWN (no uniform) in staff_zone > 2 min      →  URGENT
                         "Unidentified person behind counter"

Suppression: business hours only, no alerts within 30 min of opening.
"""
from __future__ import annotations
import time
from datetime import datetime

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.detectors import staff_identity
from app.ai.zone_logic import bbox_in_zone


# Sustained-duration thresholds.
UNAUTHORISED_SECONDS = 2 * 60     # no uniform → URGENT after 2 min
NO_NAMETAG_SECONDS   = 5 * 60     # uniform but no lanyard → INFO after 5 min

DEDUP_SECONDS = 20 * 60
OPENING_GRACE_SECONDS = 30 * 60


class StaffZoneDetector(Detector):
    detection_type = "staff_zone"
    needs_tracking = True

    def __init__(self):
        # (track_id, rule) → last alert epoch
        self._fired: dict[tuple[int, str], float] = {}

    # ------------------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        zones = [z for z in ctx.zones
                 if "staff_zone" in (z.get("detection_types_json") or [])
                 and not z.get("suppressed")]
        if not zones:
            return []

        # Business-hours + opening-grace suppression.
        if not self._business_hours_active(ctx):
            return []
        if self._in_opening_grace(ctx):
            return []

        thr = float(cfg.get("confidence_threshold", 0.5))
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr]
        now = time.time()
        out: list[DetectionEvent] = []

        for det in people:
            in_zone = any(bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"])
                          for z in zones)
            if not in_zone:
                continue
            tid = staff_identity.match_track(ctx, det)
            staff_identity.observe(ctx.camera_id, tid, "staff_zone", now)

            verdict = staff_identity.classify(ctx, det, tid, now)
            elapsed = verdict["time_in_staff_zone_s"]

            # HIGH or MEDIUM → identified staff. Mark them on the
            # staff_tracks roster and emit NO alerts for this track.
            if verdict["level"] in ("high", "medium"):
                staff_identity.mark_staff_track(
                    ctx, tid,
                    source="uniform" if verdict["top_ok"] else "zone",
                )
                # Correct-colour staff who are missing a lanyard get
                # the gentle "missing name tag" INFO after 5 min — same
                # operator action, never URGENT.
                if (verdict["top_ok"]
                        and verdict["has_lanyard"] is False
                        and elapsed >= NO_NAMETAG_SECONDS
                        and now - self._fired.get((tid, "missing_nametag"), 0) >= DEDUP_SECONDS):
                    self._fired[(tid, "missing_nametag")] = now
                    out.append(self._make_event(
                        ctx, det, tid, "missing_nametag", elapsed, "info"))
                continue

            # UNKNOWN → potential intruder / customer in staff area.
            if elapsed >= UNAUTHORISED_SECONDS:
                rule = "unauthorised_person"
                if now - self._fired.get((tid, rule), 0) >= DEDUP_SECONDS:
                    self._fired[(tid, rule)] = now
                    out.append(self._make_event(ctx, det, tid, rule, elapsed, "high"))

        staff_identity.forget_stale(now)
        return out

    # ------------------------------------------------------------------

    def _make_event(self, ctx: DetectorContext, det: dict, tid: int,
                    rule: str, duration: float, priority: str) -> DetectionEvent:
        return DetectionEvent(
            detection_type=self.detection_type, cls=rule,
            confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
            extra={
                "priority": priority,
                "rule": rule,
                "duration_seconds": int(duration),
                "duration_minutes": int(duration / 60),
                "store_id": ctx.store_id,
            },
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _business_hours_active(ctx: DetectorContext) -> bool:
        if not ctx.business_hours or not ctx.store_timezone:
            return True
        try:
            from app.utils.business_hours import is_open, localised_now
            return is_open(ctx.business_hours, localised_now(ctx.store_timezone))
        except Exception:
            return True

    @staticmethod
    def _in_opening_grace(ctx: DetectorContext) -> bool:
        if not ctx.business_hours or not ctx.store_timezone:
            return False
        try:
            from app.utils.business_hours import localised_now
            now_local = localised_now(ctx.store_timezone)
            weekday_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            windows = ctx.business_hours.get(weekday_keys[now_local.weekday()]) or []
            for win in windows:
                try:
                    a, _ = win.split("-")
                    ha, ma = (int(x) for x in a.split(":"))
                    opening = datetime(now_local.year, now_local.month, now_local.day,
                                       ha, ma, tzinfo=now_local.tzinfo)
                    delta = (now_local - opening).total_seconds()
                    if 0 <= delta <= OPENING_GRACE_SECONDS:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False
