"""Incident lifecycle housekeeping.

``incidents.attach_orphans`` folds any recent open alert that is not yet
part of an incident into one. In normal operation there are none: every
promoted detection is attached in the same transaction that writes it.
The sweep exists for the failure case (an attach that errored and was
rolled back to its savepoint), so a transient error can delay an incident
by a few minutes but can never leave an alert invisible to the incident
views.

No-op unless the lifecycle is enabled and migration 0050 is present.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

ORPHAN_LOOKBACK = timedelta(hours=2)


@celery_app.task(name="incidents.attach_orphans")
def attach_orphans() -> dict:
    from app.services.incidents import backfill_open_alerts, lifecycle_ready

    with SessionLocal() as db:
        if not lifecycle_ready(db):
            return {"skipped": "lifecycle_disabled"}
        stats = backfill_open_alerts(
            db, since=datetime.now(timezone.utc) - ORPHAN_LOOKBACK, limit=500)
        db.commit()
    if stats.get("opened") or stats.get("appended"):
        log.warning("incidents: attached orphaned alerts %s", stats)
    return stats
