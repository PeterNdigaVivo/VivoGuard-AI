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

from app.models import Alert, DetectionEvent, User
from app.schemas.alert import AlertActionOut

log = logging.getLogger(__name__)

VerdictLiteral = Literal["confirm", "dismiss"]


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
