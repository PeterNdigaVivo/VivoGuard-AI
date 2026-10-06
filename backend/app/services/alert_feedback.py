"""Single source of truth for "operator marked an alert True/False".

Used by:
  • POST /alerts/{id}/confirm  (api/alerts.py)
  • POST /alerts/{id}/dismiss  (api/alerts.py)
  • POST /labels/{id}          (Part 5 sprint endpoint)

The previous inline-duplicated pattern in confirm/dismiss had a real
hazard: any future change to the feedback-loop side effect (e.g.
the absorb_dismissed switch in commit f8ee2e8) had to be made in
THREE places to stay consistent. Factoring it here closes that gap.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    Alert, AlertReviewDecision, AssuranceCase, DetectionEvent, TrainingImage,
    User,
)
from app.schemas.alert import AlertActionOut

log = logging.getLogger(__name__)

VerdictLiteral = Literal["confirm", "dismiss"]


def record_independent_verdict(
    db: Session,
    alert_id: int,
    verdict: VerdictLiteral,
    user: User,
) -> dict:
    """Append a blind second review and govern the training evidence.

    Agreement promotes the first review's quarantined sample. Disagreement
    keeps it quarantined and opens an assurance case for adjudication. The
    alert's original operational status is never silently rewritten.
    """
    if verdict not in ("confirm", "dismiss"):
        raise HTTPException(422, "verdict must be 'confirm' or 'dismiss'")
    alert = db.get(Alert, alert_id)
    if not alert or alert.status not in {"confirmed", "dismissed"}:
        raise HTTPException(409, "alert requires a completed primary review")
    prior = (db.query(AlertReviewDecision)
             .filter(AlertReviewDecision.alert_id == alert.id)
             .order_by(AlertReviewDecision.created_at,
                       AlertReviewDecision.id).all())
    if not prior:
        raise HTTPException(409, "primary review evidence is missing")
    if any(row.reviewer_id == user.id for row in prior):
        raise HTTPException(409, "reviewer must be independent")

    second = "confirmed" if verdict == "confirm" else "dismissed"
    first = alert.status
    agreed = first == second
    db.add(AlertReviewDecision(
        alert_id=alert.id, reviewer_id=user.id, verdict=second,
        classification="independent_agreement" if agreed
        else "independent_disagreement",
        # AI verdict showing at decision time - the data needed to
        # measure verifier precision per detection_type later.
        extra={"ai_verdict": alert.ai_verdict,
               "ai_confidence": alert.ai_confidence},
    ))
    images = (db.query(TrainingImage)
              .filter(TrainingImage.source_alert_id == alert.id).all())
    for image in images:
        image.eligible_for_training = agreed
        image.review_state = "approved" if agreed else "quarantined"
        source = dict(image.source_extra or {})
        source.update({
            "independent_reviewer_id": user.id,
            "independent_review_agreed": agreed,
        })
        image.source_extra = source

    event = db.get(DetectionEvent, alert.event_id)
    if not agreed:
        alert.training_eligible = False
        alert.review_only = True
        existing = (db.query(AssuranceCase)
                    .filter(AssuranceCase.dedup_key ==
                            f"review-disagreement:{alert.id}").one_or_none())
        if existing is None:
            db.add(AssuranceCase(
                dedup_key=f"review-disagreement:{alert.id}",
                case_type="reviewer_disagreement", severity="high",
                status="open", title=f"Independent review disagreement: alert {alert.id}",
                camera_id=event.camera_id if event else None,
                alert_id=alert.id, event_id=event.id if event else None,
                root_cause="human_review_disagreement",
                evidence={"primary_verdict": first,
                          "independent_verdict": second},
                training_status="blocked_pending_adjudication",
                human_review_required=True,
            ))
    db.commit()
    if agreed and event is not None and images:
        from app.training.feedback_loop import _maybe_enqueue_training
        _maybe_enqueue_training(db, event.detection_type)
    return {
        "alert_id": alert.id,
        "primary_verdict": first,
        "independent_verdict": second,
        "agreed": agreed,
        "training_evidence_count": len(images),
        "training_eligible": bool(agreed and images),
    }


def record_verdict(
    db: Session,
    alert_id: int,
    verdict: VerdictLiteral,
    user: User,
    event: Optional[DetectionEvent] = None,   # Part 5 sprint signature
) -> AlertActionOut:
    """Flip an alert to confirmed/dismissed and reply straight away.

    In the request: the verdict, its append-only review decision and the
    alert-quality breaker. The breaker stays here on purpose: it decides
    whether THIS alert may feed training and must count this verdict
    before the next alert from the pair is created (it is a bounded query,
    so it does not slow down as history grows).

    After the commit: the training sample (snapshot copy into the dataset
    + retraining check) is handed to the `feedback.absorb_verdict` Celery
    task, so nothing that happens there can delay, undo or alter the
    operator's verdict. Idempotent — a repeat click is a no-op for
    training because the task checks `feedback_used_for_training`.

    `event` is an optional, already-fetched DetectionEvent the caller may
    pass to save a lookup (the sprint endpoint already has it).
    """
    started = time.perf_counter()
    if verdict not in ("confirm", "dismiss"):
        raise HTTPException(
            422, f"verdict must be 'confirm' or 'dismiss', got {verdict!r}")
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")

    a.status          = "confirmed" if verdict == "confirm" else "dismissed"
    a.assigned_to     = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    # Append before updating the current-state workflow. Repeated decisions by
    # the same reviewer remain auditable; agreement uses each reviewer's
    # latest decision and therefore never destroys history.
    db.add(AlertReviewDecision(
        alert_id=a.id, reviewer_id=user.id,
        verdict="confirmed" if verdict == "confirm" else "dismissed",
        # AI verdict showing at decision time - the data needed to
        # measure verifier precision per detection_type later.
        extra={"ai_verdict": a.ai_verdict,
               "ai_confidence": a.ai_confidence}))
    db.flush()

    # Recalculate the pair circuit breaker using this verdict before any
    # learning side effect. If the threshold is crossed, this alert and all
    # subsequent alerts stay evidence-only until a governed release.
    quality_started = time.perf_counter()
    ev_for_quality = event or db.get(DetectionEvent, a.event_id)
    if ev_for_quality is not None:
        from app.services.alert_quality import refresh_pair_control
        state = refresh_pair_control(
            db, ev_for_quality.camera_id, ev_for_quality.detection_type)
        if state is not None and state.mode in {"review_only", "quarantined"}:
            a.review_only = True
            a.notification_suppressed = True
            a.training_eligible = False
    quality_ms = (time.perf_counter() - quality_started) * 1000

    training_eligible = bool(a.training_eligible)
    db.commit()

    # Training sample in the background, AFTER the commit so the worker
    # sees the saved verdict.
    if training_eligible:
        _enqueue_absorb(a.id, verdict)
    else:
        log.info("alert_feedback: training skipped for quality-controlled "
                 "alert=%s", alert_id)

    log.info("alert_feedback: verdict saved alert=%s verdict=%s "
             "quality_ms=%.0f total_ms=%.0f",
             a.id, verdict, quality_ms, (time.perf_counter() - started) * 1000)
    return AlertActionOut(id=a.id, status=a.status)


def _enqueue_absorb(alert_id: int, verdict: str) -> None:
    """Queue the training-sample task. If the queue is unreachable the
    verdict still stands; the `feedback.absorb_pending` sweep picks the
    alert up later, so this only logs."""
    try:
        from app.tasks.feedback_absorb import absorb_verdict
        # One quick publish retry at most: the operator is waiting on this
        # reply, and the sweep covers a queue that is down.
        absorb_verdict.apply_async(
            args=[alert_id, verdict], retry=True,
            retry_policy={"max_retries": 1, "interval_start": 0,
                          "interval_step": 0.2, "interval_max": 0.2})
    except Exception:
        log.exception("alert_feedback: could not queue the training sample "
                      "for alert=%s verdict=%s — the 15-minute sweep will "
                      "retry", alert_id, verdict)
