"""Fail-safe quality controls for unreliable camera/detector pairs."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, Camera, DetectionEvent,
    Store,
)

MIN_REVIEWED_SAMPLES = 20
ROLLING_SAMPLE_SIZE = 50
QUARANTINE_FALSE_RATE = 0.50
RECOVERY_FALSE_RATE = 0.20
RECOVERY_SAMPLES = 20
CONTROLLED_MODES = {"active", "review_only", "quarantined"}


def _reviewed_pair(db: Session, camera_id: int, detection_type: str,
                   *, limit: int = ROLLING_SAMPLE_SIZE) -> list[Alert]:
    return (db.query(Alert)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .filter(DetectionEvent.camera_id == camera_id,
                      DetectionEvent.detection_type == detection_type,
                      Alert.status.in_(("confirmed", "dismissed")))
              .order_by(Alert.created_at.desc(), Alert.id.desc())
              .limit(limit).all())


def pair_metrics(db: Session, camera_id: int, detection_type: str) -> dict:
    reviewed = _reviewed_pair(db, camera_id, detection_type)
    false_count = sum(a.status == "dismissed" for a in reviewed)
    sample = len(reviewed)
    return {
        "sample_size": sample,
        "true_alerts": sample - false_count,
        "false_alerts": false_count,
        "false_rate": (false_count / sample) if sample else None,
    }


def refresh_pair_control(db: Session, camera_id: int,
                         detection_type: str) -> AlertQualityControl | None:
    """Open the automatic circuit breaker when reviewed evidence is poor.

    Recovery is deliberately never automatic.  A human must review at least
    ``RECOVERY_SAMPLES`` newer decisions and explicitly release the pair.
    """
    metrics = pair_metrics(db, camera_id, detection_type)
    state = (db.query(AlertQualityControl)
               .filter(AlertQualityControl.camera_id == camera_id,
                       AlertQualityControl.detection_type == detection_type)
               .first())
    if state:
        state.last_sample_size = metrics["sample_size"]
        state.last_false_rate = metrics["false_rate"]
    if (metrics["sample_size"] >= MIN_REVIEWED_SAMPLES
            and metrics["false_rate"] is not None
            and metrics["false_rate"] >= QUARANTINE_FALSE_RATE
            and (state is None or state.mode == "active")):
        if state is None:
            state = AlertQualityControl(camera_id=camera_id,
                                        detection_type=detection_type)
            db.add(state)
        state.mode = "quarantined"
        state.source = "automatic"
        state.reason = (f"rolling false-alert rate {metrics['false_rate']:.1%} "
                        f"across {metrics['sample_size']} reviewed alerts")
        state.changed_by = "system:alert-quality"
        state.changed_at = datetime.now(timezone.utc)
        state.quarantined_at = state.changed_at
        state.reviewed_count_at_quarantine = metrics["sample_size"]
        state.last_sample_size = metrics["sample_size"]
        state.last_false_rate = metrics["false_rate"]
    return state


def set_manual_mode(db: Session, camera_id: int, detection_type: str,
                    mode: str, *, reason: str, actor: str,
                    force: bool = False) -> AlertQualityControl:
    if mode not in CONTROLLED_MODES:
        raise ValueError(f"unsupported quality-control mode: {mode}")
    if not reason.strip():
        raise ValueError("a documented reason is required")
    metrics = pair_metrics(db, camera_id, detection_type)
    state = (db.query(AlertQualityControl)
               .filter(AlertQualityControl.camera_id == camera_id,
                       AlertQualityControl.detection_type == detection_type)
               .first())
    if state is None:
        state = AlertQualityControl(camera_id=camera_id,
                                    detection_type=detection_type)
        db.add(state)
    if mode == "active" and state.mode == "quarantined" and not force:
        reviewed_since = 0
        if state.quarantined_at is not None:
            reviewed_since = (db.query(Alert)
                .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                .filter(DetectionEvent.camera_id == camera_id,
                        DetectionEvent.detection_type == detection_type,
                        Alert.status.in_(("confirmed", "dismissed")),
                        Alert.created_at > state.quarantined_at).count())
        recovered = (reviewed_since >= RECOVERY_SAMPLES
                     and metrics["false_rate"] is not None
                     and metrics["false_rate"] <= RECOVERY_FALSE_RATE)
        if not recovered:
            raise ValueError(
                "release requires 20 post-quarantine reviews and a rolling "
                "false-alert rate at or below 20%; force requires an explicit reason")
    state.mode = mode
    state.source = "manual"
    state.reason = reason.strip()
    state.changed_by = actor
    state.changed_at = datetime.now(timezone.utc)
    if mode == "quarantined":
        state.quarantined_at = state.changed_at
        state.reviewed_count_at_quarantine = metrics["sample_size"]
    state.last_sample_size = metrics["sample_size"]
    state.last_false_rate = metrics["false_rate"]
    return state


def apply_quality_control(db: Session, alert: Alert,
                          event: DetectionEvent) -> AlertQualityControl | None:
    state = (db.query(AlertQualityControl)
               .filter(AlertQualityControl.camera_id == event.camera_id,
                       AlertQualityControl.detection_type == event.detection_type)
               .first())
    if state and state.mode in {"review_only", "quarantined"}:
        alert.review_only = True
        alert.notification_suppressed = True
        alert.training_eligible = False
        extra = dict(event.extra or {})
        extra["quality_control"] = {
            "mode": state.mode,
            "reason": state.reason,
            "training_eligible": False,
            "notification_suppressed": True,
        }
        event.extra = extra
    return state


def alert_is_suppressed(db: Session, alert_id: int) -> bool:
    value = (db.query(Alert.notification_suppressed)
               .filter(Alert.id == alert_id).scalar())
    return bool(value)


def quality_scorecards(db: Session, *, days: int = 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(Alert, DetectionEvent, Camera, Store)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .join(Camera, DetectionEvent.camera_id == Camera.id)
              .outerjoin(Store, Camera.store_id == Store.id)
              .filter(Alert.created_at >= cutoff).all())
    groups: dict[tuple, list] = defaultdict(list)
    for alert, event, camera, store in rows:
        groups[(camera.store_id, store.name if store else camera.site,
                camera.id, camera.name, event.detection_type)].append((alert, event))
    alert_ids = [alert.id for alert, *_rest in rows]
    decisions = (db.query(AlertReviewDecision)
                   .filter(AlertReviewDecision.alert_id.in_(alert_ids))
                   .order_by(AlertReviewDecision.created_at,
                             AlertReviewDecision.id).all()) if alert_ids else []
    # Append-only history; the latest decision from each reviewer is their
    # current position for agreement measurement.
    latest_by_alert_reviewer = {}
    for decision in decisions:
        latest_by_alert_reviewer[(decision.alert_id,
                                  decision.reviewer_id)] = decision.verdict
    cards = []
    for key, samples in groups.items():
        confirmed = sum(a.status == "confirmed" for a, _ in samples)
        dismissed = sum(a.status == "dismissed" for a, _ in samples)
        unreviewed = len(samples) - confirmed - dismissed
        reviewed = confirmed + dismissed
        clips = sum(bool(e.clip_path) for _, e in samples)
        multi_review = []
        for alert, _event in samples:
            verdicts = [verdict for (alert_id, _reviewer), verdict
                        in latest_by_alert_reviewer.items()
                        if alert_id == alert.id]
            if len(verdicts) >= 2:
                multi_review.append(len(set(verdicts)) == 1)
        agreements = sum(multi_review)
        disagreements = len(multi_review) - agreements
        state = (db.query(AlertQualityControl)
                   .filter(AlertQualityControl.camera_id == key[2],
                           AlertQualityControl.detection_type == key[4]).first())
        cards.append({
            "store_id": key[0], "store_name": key[1],
            "camera_id": key[2], "camera_name": key[3],
            "detection_type": key[4], "total_alerts": len(samples),
            "true_alerts": confirmed, "false_alerts": dismissed,
            "unreviewed_alerts": unreviewed, "reviewed_sample_size": reviewed,
            "precision": (confirmed / reviewed) if reviewed else None,
            "recall": None,
            "recall_limitation": "Requires independently reported missed events; alerts alone cannot measure recall.",
            "incident_clips_available": clips,
            "clip_availability_rate": clips / len(samples),
            "multi_reviewer_alerts": len(multi_review),
            "reviewer_agreement": (agreements / len(multi_review)
                                   if multi_review else None),
            "reviewer_agreement_count": agreements,
            "reviewer_disagreement_count": disagreements,
            "reviewer_agreement_limitation": (
                None if multi_review else
                "No alerts in this slice have decisions from at least two distinct reviewers."),
            "quality_mode": state.mode if state else "active",
            "notification_suppressed": bool(state and state.mode != "active"),
        })
    return sorted(cards, key=lambda c: (c["store_name"] or "", c["camera_id"],
                                        c["detection_type"]))
