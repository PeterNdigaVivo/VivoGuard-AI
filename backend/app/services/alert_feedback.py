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
    """Flip an alert to confirmed/dismissed AND fire the feedback-loop
    side effect that grows the training pool. Idempotent — calling
    again on an already-confirmed alert is a no-op (the absorb_*
    helpers guard on `feedback_used_for_training`).

    `event` is an optional, already-fetched DetectionEvent the caller
    may pass to avoid a second round-trip (the sprint endpoint joins
    Alert→DetectionEvent for dwell-display anyway). When None, the
    feedback-loop helpers do their own lookups.
    """
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
        verdict="confirmed" if verdict == "confirm" else "dismissed"))
    db.flush()

    # Recalculate the pair circuit breaker using this verdict before any
    # learning side effect. If the threshold is crossed, this alert and all
    # subsequent alerts stay evidence-only until a governed release.
    ev_for_quality = event or db.get(DetectionEvent, a.event_id)
    if ev_for_quality is not None:
        from app.services.alert_quality import refresh_pair_control
        state = refresh_pair_control(
            db, ev_for_quality.camera_id, ev_for_quality.detection_type)
        if state is not None and state.mode in {"review_only", "quarantined"}:
            a.review_only = True
            a.notification_suppressed = True
            a.training_eligible = False

    # Feedback-loop side effect — the actual reason rule #2 exists.
    # Best-effort: never block the operator's verdict on a feedback-
    # loop failure, but DO log it (the previous inline handlers
    # swallowed silently — switched to log.exception per the dry-run
    # decision).
    try:
        if not a.training_eligible:
            log.info("alert_feedback: training skipped for quality-controlled "
                     "alert=%s", alert_id)
        elif verdict == "confirm":
            from app.training.feedback_loop import absorb_confirmed
            absorb_confirmed(db, a.id)
        else:
            from app.training.feedback_loop import mark_dismissed
            mark_dismissed(db, a.id)
    except Exception:
        log.exception("alert_feedback: feedback-loop side effect failed "
                      "alert=%s verdict=%s", alert_id, verdict)

    db.commit()
    # `event` arg accepted for API stability with the sprint caller; not
    # currently used by record_verdict itself (the absorb_* helpers look
    # up DetectionEvent from alert.event_id internally). Reserved for
    # future enrichment without changing this signature again.
    _ = event
    return AlertActionOut(id=a.id, status=a.status)
