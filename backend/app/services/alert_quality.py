"""Fail-safe quality controls for unreliable camera/detector pairs."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import sqrt

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, AssuranceCase, Camera,
    DetectionEvent, EvidenceManifest, RecordingClip, Store,
)

MIN_REVIEWED_SAMPLES = 20
ROLLING_SAMPLE_SIZE = 50
QUARANTINE_FALSE_RATE = 0.50
RECOVERY_FALSE_RATE = 0.20
RECOVERY_SAMPLES = 20
CONTROLLED_MODES = {"active", "review_only", "quarantined"}
TARGET_PRECISION = 0.99
TARGET_MIN_REVIEWED = 300
TARGET_MIN_RECALL_EVENTS = 300
ONE_SIDED_95_Z = 1.6448536269514722


def _wilson_lower_bound(successes: int, total: int) -> float | None:
    """One-sided 95% Wilson lower bound for reviewed precision."""
    if total <= 0:
        return None
    proportion = successes / total
    z2 = ONE_SIDED_95_Z ** 2
    denominator = 1 + z2 / total
    centre = proportion + z2 / (2 * total)
    margin = ONE_SIDED_95_Z * sqrt(
        (proportion * (1 - proportion) + z2 / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def _reviewed_pair(db: Session, camera_id: int, detection_type: str,
                   *, limit: int = ROLLING_SAMPLE_SIZE) -> list[tuple[Alert, str]]:
    rows = (db.query(Alert, AlertReviewDecision)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .outerjoin(AlertReviewDecision,
                         AlertReviewDecision.alert_id == Alert.id)
              .filter(DetectionEvent.camera_id == camera_id,
                      DetectionEvent.detection_type == detection_type,
                      or_(Alert.status.in_(("confirmed", "dismissed")),
                          AlertReviewDecision.id.is_not(None)))
              .order_by(Alert.created_at.desc(), Alert.id.desc(),
                        AlertReviewDecision.created_at,
                        AlertReviewDecision.id).all())
    # Alert.status is mutable lifecycle state (for example `resolved`). The
    # latest append-only decision remains the authoritative human verdict.
    alerts: dict[int, Alert] = {}
    verdicts: dict[int, str] = {}
    order: list[int] = []
    for alert, decision in rows:
        if alert.id not in alerts:
            alerts[alert.id] = alert
            order.append(alert.id)
        if decision is not None:
            verdicts[alert.id] = decision.verdict
    reviewed = []
    for alert_id in order:
        alert = alerts[alert_id]
        verdict = verdicts.get(alert_id, alert.status)
        if verdict in {"confirmed", "dismissed"}:
            reviewed.append((alert, verdict))
        if len(reviewed) >= limit:
            break
    return reviewed


def pair_metrics(db: Session, camera_id: int, detection_type: str) -> dict:
    reviewed = _reviewed_pair(db, camera_id, detection_type)
    false_count = sum(verdict == "dismissed" for _alert, verdict in reviewed)
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
            decision_ids = {row[0] for row in (db.query(
                AlertReviewDecision.alert_id)
                .join(Alert, Alert.id == AlertReviewDecision.alert_id)
                .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                .filter(DetectionEvent.camera_id == camera_id,
                        DetectionEvent.detection_type == detection_type,
                        AlertReviewDecision.created_at > state.quarantined_at)
                .distinct().all())}
            legacy_ids = {row[0] for row in (db.query(Alert.id)
                .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                .filter(DetectionEvent.camera_id == camera_id,
                        DetectionEvent.detection_type == detection_type,
                        Alert.status.in_(("confirmed", "dismissed")),
                        Alert.created_at > state.quarantined_at).all())}
            reviewed_since = len(decision_ids | legacy_ids)
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
              .filter(Alert.created_at >= cutoff,
                      DetectionEvent.detection_type != "positive_operational").all())
    groups: dict[tuple, list] = defaultdict(list)
    for alert, event, camera, store in rows:
        groups[(camera.store_id, store.name if store else camera.site,
                camera.id, camera.name, event.detection_type)].append((alert, event))
    recall_evidence: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"windows": 0, "true_positives": 0, "false_negatives": 0})
    recall_rows = (db.query(AssuranceCase, Camera, Store)
                   .join(Camera, Camera.id == AssuranceCase.camera_id)
                   .outerjoin(Store, Store.id == Camera.store_id)
                   .filter(AssuranceCase.case_type == "recall_sample",
                           AssuranceCase.reviewed_at >= cutoff,
                           AssuranceCase.status == "resolved").all())
    for case_row, camera, store in recall_rows:
        final_label = (case_row.label_json or {}).get("final_event_label")
        key = (camera.store_id, store.name if store else camera.site,
               camera.id, camera.name, final_label)
        if final_label:
            groups.setdefault(key, [])
            recall_evidence[key]["windows"] += 1
            if case_row.root_cause == "recall_true_positive":
                recall_evidence[key]["true_positives"] += 1
            elif case_row.root_cause == "recall_false_negative":
                recall_evidence[key]["false_negatives"] += 1
    alert_ids = [alert.id for alert, *_rest in rows]
    decisions = (db.query(AlertReviewDecision)
                   .filter(AlertReviewDecision.alert_id.in_(alert_ids))
                   .order_by(AlertReviewDecision.created_at,
                             AlertReviewDecision.id).all()) if alert_ids else []
    manifests = {
        row.alert_id: row
        for row in (db.query(EvidenceManifest)
                    .filter(EvidenceManifest.alert_id.in_(alert_ids)).all())
    } if alert_ids else {}
    # Recording rows are retained after file pruning and therefore provide a
    # durable eligibility denominator for legacy alerts whose shadow manifest
    # predates explicit clip_eligible values.
    recordings = (db.query(RecordingClip)
                    .filter(RecordingClip.started_at <= datetime.now(timezone.utc),
                            or_(RecordingClip.ended_at.is_(None),
                                RecordingClip.ended_at >= cutoff)).all())
    # Append-only history; the latest decision from each reviewer is their
    # current position for agreement measurement.
    latest_by_alert_reviewer = {}
    latest_decision_by_alert = {}
    for decision in decisions:
        latest_by_alert_reviewer[(decision.alert_id,
                                  decision.reviewer_id)] = decision.verdict
        latest_decision_by_alert[decision.alert_id] = decision.verdict
    cards = []
    for key, samples in groups.items():
        verdicts = {
            alert.id: latest_decision_by_alert.get(alert.id, alert.status)
            for alert, _event in samples
        }
        confirmed = sum(verdict == "confirmed" for verdict in verdicts.values())
        dismissed = sum(verdict == "dismissed" for verdict in verdicts.values())
        unreviewed = len(samples) - confirmed - dismissed
        reviewed = confirmed + dismissed
        lower_bound = _wilson_lower_bound(confirmed, reviewed)
        precision_gate = bool(
            reviewed >= TARGET_MIN_REVIEWED
            and lower_bound is not None
            and lower_bound >= TARGET_PRECISION)
        # Legacy recorder paths were persisted in event.extra before the
        # canonical clip_path column was wired for every detector.  Counting
        # only the column understates evidence availability and can block a
        # release gate even when a playable incident clip exists.
        clips = 0
        clip_eligible = 0
        clip_ineligible = 0
        clip_unknown = 0
        for alert, event in samples:
            available = bool(
                event.clip_path or (event.extra or {}).get("alert_clip_path"))
            manifest = manifests.get(alert.id)
            eligibility = manifest.clip_eligible if manifest else None
            # A persisted playable path is conclusive evidence that this alert
            # was eligible, including pre-manifest legacy alerts.
            if available:
                eligibility = True
                clips += 1
            elif eligibility is None and event.timestamp is not None:
                event_at = (event.timestamp.replace(tzinfo=timezone.utc)
                            if event.timestamp.tzinfo is None else event.timestamp)
                covered = False
                for recording in recordings:
                    started = (recording.started_at.replace(tzinfo=timezone.utc)
                               if recording.started_at.tzinfo is None
                               else recording.started_at)
                    ended = recording.ended_at
                    if ended is not None and ended.tzinfo is None:
                        ended = ended.replace(tzinfo=timezone.utc)
                    same_source = recording.camera_id == event.camera_id
                    if (event.detection_type == "shop_open_close"
                            and key[0] is not None):
                        same_source = (same_source
                                       or recording.store_id == key[0])
                    if (same_source and started <= event_at
                            and (ended is None or ended >= event_at)):
                        covered = True
                        break
                eligibility = covered
            if eligibility is True:
                clip_eligible += 1
            elif eligibility is False:
                clip_ineligible += 1
            else:
                clip_unknown += 1
        multi_review = []
        for alert, _event in samples:
            reviewer_verdicts = [
                verdict for (alert_id, _reviewer), verdict
                in latest_by_alert_reviewer.items()
                if alert_id == alert.id
            ]
            if len(reviewer_verdicts) >= 2:
                multi_review.append(len(set(reviewer_verdicts)) == 1)
        agreements = sum(multi_review)
        disagreements = len(multi_review) - agreements
        recall_counts = recall_evidence[key]
        recall_events = (recall_counts["true_positives"]
                         + recall_counts["false_negatives"])
        measured_recall = (recall_counts["true_positives"] / recall_events
                           if recall_events else None)
        recall_lower_bound = _wilson_lower_bound(
            recall_counts["true_positives"], recall_events)
        recall_gate = bool(
            recall_events >= TARGET_MIN_RECALL_EVENTS
            and recall_lower_bound is not None
            and recall_lower_bound >= TARGET_PRECISION)
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
            "precision_lower_bound_95": lower_bound,
            "target_precision": TARGET_PRECISION,
            "target_minimum_reviewed": TARGET_MIN_REVIEWED,
            "target_99_precision_evidence_met": precision_gate,
            "target_99_evidence_met": precision_gate and recall_gate,
            "recall": measured_recall,
            "recall_lower_bound_95": recall_lower_bound,
            "target_minimum_recall_events": TARGET_MIN_RECALL_EVENTS,
            "target_99_recall_evidence_met": recall_gate,
            "recall_target_event_windows": recall_counts["windows"],
            "recall_true_positive_events": recall_counts["true_positives"],
            "recall_false_negative_events": recall_counts["false_negatives"],
            "recall_limitation": (
                None if measured_recall is not None else
                "Requires independently double-reviewed random footage containing target events and missed events; alerts alone cannot measure recall."),
            "incident_clips_available": clips,
            "clip_eligible_alerts": clip_eligible,
            "clip_ineligible_alerts": clip_ineligible,
            "clip_eligibility_unknown": clip_unknown,
            "clip_availability_rate": (
                clips / clip_eligible if clip_eligible else None),
            "clip_availability_limitation": (
                None if not clip_unknown else
                "Alerts with unknown clip eligibility are excluded from the evidence-SLA denominator."),
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
