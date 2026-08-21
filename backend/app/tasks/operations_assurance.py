"""Scheduled deterministic assurance agents; all outputs require human review."""
import logging
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import AssuranceCase, GovernanceAuditLog, OperationalEvent, RiskReview
from app.operations.assurance import (
    assess_coverage, correlate_event, create_alert_quality_cases,
    create_lone_worker_cases,
)
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="operations.coverage_assurance", ignore_result=True)
def coverage_assurance():
    with SessionLocal() as db:
        results = assess_coverage(db, persist=True)
        db.commit()
        log.info("coverage assurance: %s requirements assessed", len(results))


@celery_app.task(name="operations.alert_quality", ignore_result=True)
def alert_quality():
    with SessionLocal() as db:
        count = create_alert_quality_cases(db)
        db.commit()
        log.info("alert quality: %s exceptions", count)


@celery_app.task(name="operations.lone_worker", ignore_result=True)
def lone_worker():
    with SessionLocal() as db:
        count = create_lone_worker_cases(db)
        db.commit()
        log.info("lone worker: %s review cases", count)


@celery_app.task(name="operations.event_fusion", ignore_result=True)
def event_fusion():
    """Recover imported events that did not complete synchronous correlation."""
    with SessionLocal() as db:
        events = (db.query(OperationalEvent)
                  .outerjoin(RiskReview, RiskReview.operational_event_id == OperationalEvent.id)
                  .filter(RiskReview.id.is_(None))
                  .order_by(OperationalEvent.occurred_at).limit(500).all())
        for event in events:
            correlate_event(db, event)
        db.commit()
        log.info("event fusion: %s pending events correlated", len(events))


@celery_app.task(name="operations.retention", ignore_result=True)
def retention():
    """Delete only closed/reviewed records after documented retention windows."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        resolved_cases = (db.query(AssuranceCase)
                          .filter(AssuranceCase.resolved_at < now - timedelta(days=365))
                          .delete(synchronize_session=False))
        reviewed_events = (db.query(OperationalEvent)
                           .filter(OperationalEvent.received_at < now - timedelta(days=365),
                                   OperationalEvent.id.in_(db.query(RiskReview.operational_event_id)
                                                          .filter(RiskReview.reviewed_at.is_not(None))))
                           .delete(synchronize_session=False))
        audit_rows = (db.query(GovernanceAuditLog)
                      .filter(GovernanceAuditLog.created_at < now - timedelta(days=730))
                      .delete(synchronize_session=False))
        db.commit()
        log.info("operations retention: cases=%s events=%s audit=%s",
                 resolved_cases, reviewed_events, audit_rows)
