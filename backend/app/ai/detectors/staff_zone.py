"""StaffZoneDetector — behind-counter (staff-only) area compliance.

A camera operator tags a polygon with `staff_zone` to mark the space
BEHIND the service counter where only uniformed staff should be.
This is distinct from the customer-facing `counter` zone the
QueueDetector and StaffPresenceDetector watch.

Three rules, each gated on sustained-duration + dedup:

  1. Unidentified person behind counter (no uniform) — URGENT
       sustained 2 min → "Unidentified Person Behind Counter"
  2. Staff in uniform but missing lanyard/name tag — ATTENTION
       sustained 5 min → "Staff Member Missing Name Tag"
  3. Customer (no uniform) entered the staff area — URGENT
       fires within 30 s of first sighting → "Customer in Staff-
       Only Area"

Suppression: business-hours only, no alerts within 30 min of
opening (staff setup), per-track-per-rule dedup max once / 20 min.
"""
from __future__ import annotations
import time
from datetime import datetime

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone, iou
from app.ai.detectors.uniform_compliance import uniform_colour_score


# Sustained-duration thresholds.
UNAUTHORISED_SECONDS = 2 * 60     # no uniform → urgent after 2 min
NO_NAMETAG_SECONDS   = 5 * 60     # uniform but no lanyard/tag → 5 min
CUSTOMER_SECONDS     = 30         # no uniform + entered quickly → 30 s

# Per (track, rule) dedup window.
DEDUP_SECONDS = 20 * 60

# Score bands matching UniformComplianceDetector's _classify().
SCORE_COMPLIANT = 0.80    # >= → uniform_ok
SCORE_PARTIAL   = 0.50    # >= → no_lanyard
# < SCORE_PARTIAL → no uniform at all (customer / intruder)

# Suppress alerts for the first half-hour after the store opens
# (staff setting up; cleaning crews shuttling between back office
# and counter).
OPENING_GRACE_SECONDS = 30 * 60


class StaffZoneDetector(Detector):
    detection_type = "staff_zone"
    needs_tracking = True

    def __init__(self):
        # (track_id, rule) → last alert epoch
        self._fired: dict[tuple[int, str], float] = {}
        # track_id → first sighting epoch in this run
        self._first_seen: dict[int, float] = {}

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
        active_tids: set[int] = set()

        for det in people:
            in_zone = any(bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"])
                          for z in zones)
            if not in_zone:
                continue
            tid = self._match_track(ctx, det)
            active_tids.add(tid)
            first = self._first_seen.setdefault(tid, now)
            duration = now - first

            score = uniform_colour_score(ctx.frame_bgr, det["bbox_norm"])
            # When pixels can't be read we treat them as "uniformed
            # staff" — same conservative default as elsewhere — so we
            # never false-alarm on a camera that can't see colour.
            if score is None:
                continue

            rule, threshold = self._classify(score, duration)
            if rule is None:
                continue
            if duration < threshold:
                continue
            if now - self._fired.get((tid, rule), 0) < DEDUP_SECONDS:
                continue
            self._fired[(tid, rule)] = now
            out.append(self._make_event(ctx, det, tid, rule, duration))

        # Forget tracks that left the zone so the next entry starts a
        # fresh duration clock.
        for tid in list(self._first_seen.keys()):
            if tid not in active_tids:
                self._first_seen.pop(tid, None)
        return out

    # ------------------------------------------------------------------

    @staticmethod
    def _classify(score: float, duration: float) -> tuple[str | None, float]:
        """Map (uniform-score, time-in-zone) to (rule, threshold).
        Returns (None, 0) when the situation doesn't warrant an alert."""
        if score < SCORE_PARTIAL:
            # No uniform — customer or unidentified intruder.
            if duration <= CUSTOMER_SECONDS * 2:
                # Quick entry — likely a customer who strayed in.
                return "customer_in_staff_zone", CUSTOMER_SECONDS
            return "unauthorised_person", UNAUTHORISED_SECONDS
        if score < SCORE_COMPLIANT:
            # Uniform present but lanyard/name tag missing.
            return "missing_nametag", NO_NAMETAG_SECONDS
        # Fully compliant — no alert.
        return None, 0.0

    def _make_event(self, ctx: DetectorContext, det: dict, tid: int,
                    rule: str, duration: float) -> DetectionEvent:
        priority = "info" if rule == "missing_nametag" else "high"
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

    def _match_track(self, ctx: DetectorContext, det: dict) -> int:
        for tr, _ in ctx.tracks:
            if tr.cls in COCO_PERSON and iou(tr.bbox_norm, det["bbox_norm"]) > 0.3:
                return tr.track_id
        return hash(tuple(round(c, 2) for c in det["bbox_norm"])) & 0x7FFFFFFF

    # ------------------------------------------------------------------

    @staticmethod
    def _business_hours_active(ctx: DetectorContext) -> bool:
        if not ctx.business_hours or not ctx.store_timezone:
            return True   # no hours configured → don't block alerts
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
