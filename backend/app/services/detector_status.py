"""What each detector is really doing across the fleet.

The catalogue used to label almost everything "Active" from a hard-coded
table, whether or not any camera ran it, had the zone it needs, had store
hours, or had ever been checked against operator reviews. This computes
one honest state per detector from configuration and review evidence:

    training_required  needs a model the fleet does not have
    disabled           explicitly switched off on every camera that has it
    available          supported, but not configured on any camera
    review_only        runs, but its alerts go to the review queue (generic
                       detectors until validated, or quarantined cameras)
    validated          configured, and reviewed alerts meet the bar
    configured         configured, not yet validated

plus the numbers an operator needs and ONE recommended next action.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.zone_purposes import ZONE_TAG_DETECTORS, detector_types_for_zone_tags

# Mirrors inference_worker._load_camera_state's ALWAYS_ON set.
ALWAYS_ON = frozenset({"occupancy_metrics", "heatmap", "unique_visitor",
                       "customer_journey"})
# Detectors whose meaning depends on the store's opening hours.
SCHEDULE_REQUIRED = frozenset({"intrusion", "person", "shop_open_close", "trespass"})

STATES = ("available", "configured", "validated", "review_only",
          "training_required", "disabled")

VALIDATION_MIN_REVIEWED = 50
VALIDATION_MIN_PRECISION = 0.90
QUALITY_WINDOW_DAYS = 30


def _tags_for(detector: str) -> list[str]:
    return sorted(t for t, dets in ZONE_TAG_DETECTORS.items() if detector in dets)


def gather(db: Session) -> dict:
    """Bulk-read everything compute() needs (a handful of queries)."""
    from app.models import (
        Alert, AlertQualityControl, AlertReviewDecision, Camera, DetectionConfig,
        DetectionEvent, Store, Zone,
    )

    cams = (db.query(Camera.id, Camera.store_id, Camera.ai_enabled)
              .filter(Camera.is_deleted.is_(False)).all())
    active_ids = {c.id for c in cams if c.ai_enabled}
    store_of = {c.id: c.store_id for c in cams}
    stores_with_hours = {s.id for s in db.query(Store.id, Store.business_hours_json)
                         if s.business_hours_json}

    enabled: dict[str, set[int]] = defaultdict(set)
    disabled: dict[str, set[int]] = defaultdict(set)
    for cam_id, dtype, on in db.query(DetectionConfig.camera_id,
                                      DetectionConfig.detection_type,
                                      DetectionConfig.enabled):
        if cam_id in active_ids:
            (enabled if on else disabled)[dtype].add(cam_id)

    via_zone: dict[str, set[int]] = defaultdict(set)
    for cam_id, tags, suppressed in db.query(Zone.camera_id, Zone.detection_types_json,
                                             Zone.suppressed):
        if cam_id in active_ids and not suppressed:
            for dtype in detector_types_for_zone_tags(tags or []):
                via_zone[dtype].add(cam_id)

    since = datetime.now(timezone.utc) - timedelta(days=QUALITY_WINDOW_DAYS)
    latest = (db.query(AlertReviewDecision.alert_id,
                       func.max(AlertReviewDecision.id).label("mid"))
                .group_by(AlertReviewDecision.alert_id).subquery())
    reviews: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0})
    for dtype, verdict, n in (
            db.query(DetectionEvent.detection_type, AlertReviewDecision.verdict,
                     func.count(Alert.id))
              .join(Alert, Alert.event_id == DetectionEvent.id)
              .join(latest, latest.c.alert_id == Alert.id)
              .join(AlertReviewDecision, AlertReviewDecision.id == latest.c.mid)
              .filter(DetectionEvent.timestamp >= since)
              .group_by(DetectionEvent.detection_type, AlertReviewDecision.verdict)):
        if verdict == "confirmed":
            reviews[dtype]["tp"] += int(n)
        elif verdict == "dismissed":
            reviews[dtype]["fp"] += int(n)

    quarantined: dict[str, int] = defaultdict(int)
    for dtype, mode in db.query(AlertQualityControl.detection_type,
                                AlertQualityControl.mode):
        if mode in ("review_only", "quarantined"):
            quarantined[dtype] += 1

    return {"active_ids": active_ids, "store_of": store_of,
            "stores_with_hours": stores_with_hours, "enabled": enabled,
            "disabled": disabled, "via_zone": via_zone, "reviews": reviews,
            "quarantined": quarantined}


def compute(detector: str, meta: dict, data: dict) -> dict:
    """One detector's operator-facing status (pure; see module doc)."""
    from app.services.alert_policy import DEFAULT_REVIEW_ONLY_TYPES

    capability = meta.get("status", "active")
    needs_zone = bool(meta.get("needs_zone"))
    configured = set(data["enabled"].get(detector, set())) | set(
        data["via_zone"].get(detector, set()))
    if detector in ALWAYS_ON:
        configured |= set(data["active_ids"])
    zoned = set(data["via_zone"].get(detector, set()))
    missing_zone = sorted(configured - zoned) if needs_zone else []
    needs_schedule = detector in SCHEDULE_REQUIRED
    missing_hours_stores = sorted({
        data["store_of"].get(c) for c in configured
        if data["store_of"].get(c) is not None
        and data["store_of"].get(c) not in data["stores_with_hours"]}
    ) if needs_schedule else []

    rv = data["reviews"].get(detector, {"tp": 0, "fp": 0})
    reviewed = rv["tp"] + rv["fp"]
    precision = (rv["tp"] / reviewed) if reviewed else None
    validated_by_reviews = (reviewed >= VALIDATION_MIN_REVIEWED
                            and precision is not None
                            and precision >= VALIDATION_MIN_PRECISION)
    policy_validated = detector in set(
        getattr(settings, "policy_validated_detectors", None) or ())
    review_only = ((detector in DEFAULT_REVIEW_ONLY_TYPES and not policy_validated)
                   or data["quarantined"].get(detector, 0) > 0)

    if capability == "training_required":
        state = "training_required"
    elif not configured and data["disabled"].get(detector):
        state = "disabled"
    elif not configured:
        state = "available"
    elif review_only:
        state = "review_only"
    elif validated_by_reviews and not missing_zone and not missing_hours_stores:
        state = "validated"
    else:
        state = "configured"

    if state == "training_required":
        action = "Collect and review training examples before this can run"
    elif state == "disabled":
        action = "Switched off everywhere — enable it on a camera if a store needs it"
    elif state == "available":
        action = ("Draw the required zone on a camera to start it" if needs_zone
                  else "Turn it on for the cameras that need it")
    elif missing_zone:
        action = f"Draw the required zone on {len(missing_zone)} camera(s)"
    elif missing_hours_stores:
        action = f"Set opening hours for {len(missing_hours_stores)} store(s)"
    elif state == "review_only" and detector in DEFAULT_REVIEW_ONLY_TYPES:
        if validated_by_reviews:
            action = "Accuracy meets the bar — ask an administrator to allow live alerts"
        else:
            action = (f"Review its alerts to prove accuracy "
                      f"({reviewed}/{VALIDATION_MIN_REVIEWED} reviewed)")
    elif state == "review_only":
        action = (f"{data['quarantined'][detector]} camera(s) held for review "
                  "after false alerts — check those cameras' view and zones")
    elif state == "validated":
        action = "None"
    elif precision is not None and reviewed >= 10 and precision < VALIDATION_MIN_PRECISION:
        action = (f"Accuracy is {precision:.0%} — check zones and camera views "
                  "where it is dismissed most")
    else:
        action = (f"Review more alerts to validate "
                  f"({reviewed}/{VALIDATION_MIN_REVIEWED} reviewed)")

    return {
        "state": state,
        "configured_cameras": len(configured),
        "configured_anywhere": bool(configured),
        "needs_zone": needs_zone,
        "zone_tags": _tags_for(detector) if needs_zone else [],
        "cameras_missing_zone": len(missing_zone),
        "needs_schedule": needs_schedule,
        "stores_missing_hours": len(missing_hours_stores),
        "requirements_met": not missing_zone and not missing_hours_stores,
        "reviewed_30d": reviewed,
        "precision_30d": round(precision, 3) if precision is not None else None,
        "validation_bar": {"min_reviewed": VALIDATION_MIN_REVIEWED,
                           "min_precision": VALIDATION_MIN_PRECISION},
        "next_action": action,
    }
