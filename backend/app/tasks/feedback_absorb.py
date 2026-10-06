"""Turn an operator's True/False verdict into a training sample — in the
background, after the verdict is saved.

POST /alerts/{id}/confirm and /dismiss used to copy the snapshot into the
training dataset and run the retraining check before replying, which made
the buttons feel slow. Now the request only saves the verdict (and runs
the alert-quality breaker, which must see the verdict before the next
alert is created) and enqueues `feedback.absorb_verdict`.

Guarantees:
  • The task never changes the alert's status — a failure here can only
    delay a training sample, never undo or alter the operator's verdict.
  • It re-reads the alert first and does nothing if the verdict changed
    since the click (e.g. an Undo), if the sample already exists, or if
    the alert-quality breaker made the alert evidence-only.
  • Failures are logged and retried with growing delays; anything still
    missing (Redis down at click time, a worker restart, retries spent) is
    picked up by the `feedback.absorb_pending` sweep every 15 minutes.

Runs on the `alerts` worker, which mounts the thumbnails and datasets
volumes the copy needs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BASE_SECONDS = 30          # 30 s, 60 s, 2 min, 4 min, 8 min …
RETRY_MAX_SECONDS = 15 * 60
SWEEP_WINDOW = timedelta(days=7)
# Leave a verdict to its own task (and its retries, ~16 min in total)
# before the sweep touches it, so the two never run for the same alert.
SWEEP_MIN_AGE = timedelta(minutes=30)
SWEEP_LIMIT = 200

_EXPECTED_STATUS = {"confirm": "confirmed", "dismiss": "dismissed"}


def retry_delay_seconds(retries_done: int) -> int:
    """Back-off before the next attempt (retries_done = 0 for the first)."""
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** retries_done))


def absorb_verdict_now(alert_id: int, verdict: str, session_factory=None) -> str:
    """Create the training sample for one verdict. Returns what happened:
    absorbed | verdict_changed | already_done | not_eligible | missing.
    Raises on unexpected errors (the Celery task retries them)."""
    if verdict not in _EXPECTED_STATUS:
        raise ValueError(f"verdict must be 'confirm' or 'dismiss', got {verdict!r}")
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    from app.models import Alert
    from app.training.feedback_loop import absorb_confirmed, absorb_dismissed

    with session_factory() as db:
        # Row lock (Postgres) so two runs for one alert cannot both create
        # a sample; released by the absorb helper's commit.
        alert = (db.query(Alert).filter(Alert.id == alert_id)
                   .with_for_update().one_or_none())
        if alert is None:
            log.warning("feedback: alert %s no longer exists — no training sample", alert_id)
            return "missing"
        if alert.status != _EXPECTED_STATUS[verdict]:
            log.info("feedback: alert %s is now %r, not %r — verdict changed since "
                     "the click, no training sample", alert_id, alert.status,
                     _EXPECTED_STATUS[verdict])
            return "verdict_changed"
        if alert.feedback_used_for_training:
            return "already_done"
        if not alert.training_eligible:
            log.info("feedback: training skipped for quality-controlled alert=%s", alert_id)
            return "not_eligible"
        try:
            if verdict == "confirm":
                absorb_confirmed(db, alert_id)
            else:
                absorb_dismissed(db, alert_id)
        except Exception:
            db.rollback()
            raise
        return "absorbed"


@celery_app.task(name="feedback.absorb_verdict", bind=True, ignore_result=True,
                 max_retries=MAX_RETRIES, acks_late=True)
def absorb_verdict(self, alert_id: int, verdict: str) -> str | None:
    try:
        return absorb_verdict_now(alert_id, verdict)
    except Exception as exc:
        done = int(self.request.retries or 0)
        if done >= MAX_RETRIES:
            log.exception("feedback: giving up on alert=%s verdict=%s after %d "
                          "attempts — the 15-minute sweep will try again",
                          alert_id, verdict, done + 1)
            return None
        delay = retry_delay_seconds(done)
        log.warning("feedback: training sample for alert=%s verdict=%s failed "
                    "(attempt %d/%d): %s — retrying in %ss",
                    alert_id, verdict, done + 1, MAX_RETRIES + 1, exc, delay)
        raise self.retry(exc=exc, countdown=delay)


def pending_verdicts(db, now: datetime | None = None,
                     limit: int = SWEEP_LIMIT) -> list[tuple[int, str]]:
    """Recent human True/False verdicts that still have no training sample.

    Only real verdicts count: an AlertReviewDecision must exist, and
    /resolve's "confirmed" (which sets resolved_at) is not a True verdict.
    Newest first, so alerts that can never be absorbed (no snapshot) cannot
    crowd new ones out of the batch."""
    from sqlalchemy import exists

    from app.models import Alert, AlertReviewDecision
    now = now or datetime.now(timezone.utc)
    rows = (db.query(Alert.id, Alert.status)
              .filter(Alert.status.in_(("confirmed", "dismissed")),
                      Alert.feedback_used_for_training.is_(False),
                      Alert.training_eligible.is_(True),
                      Alert.resolved_at.is_(None),
                      Alert.acknowledged_at >= now - SWEEP_WINDOW,
                      Alert.acknowledged_at <= now - SWEEP_MIN_AGE,
                      exists().where(AlertReviewDecision.alert_id == Alert.id))
              .order_by(Alert.acknowledged_at.desc())
              .limit(limit).all())
    return [(alert_id, "confirm" if status == "confirmed" else "dismiss")
            for alert_id, status in rows]


@celery_app.task(name="feedback.absorb_pending", ignore_result=True)
def absorb_pending() -> int:
    """Safety net: re-enqueue verdicts whose training sample is missing."""
    from app.database import SessionLocal
    with SessionLocal() as db:
        pending = pending_verdicts(db)
    for alert_id, verdict in pending:
        absorb_verdict.apply_async(args=[alert_id, verdict])
    if pending:
        log.info("feedback: sweep re-enqueued %d verdict(s) without a training sample",
                 len(pending))
    return len(pending)
