"""Five-minute accountability monitor for all registered agents."""
import time

from app.agent_control.accountability import scorecards
from app.agent_control.workstreams import workstream_statuses
from app.database import SessionLocal
from app.tasks.celery_app import celery_app


@celery_app.task(name="agents.accountability", ignore_result=True)
def accountability():
    started = time.time()
    with SessionLocal() as db:
        cards = scorecards(db, persist_cases=True)
        workstreams = workstream_statuses(db, persist_cases=True)
        db.commit()
    breaches = [card for card in cards if not card["compliant"]]
    incomplete = [item for item in workstreams if not item["complete"]]
    overdue = [item for item in incomplete if item["deadline_breached"]]
    from app.tasks.agents import _write_report
    status = "critical" if (breaches or overdue) else ("warning" if incomplete else "ok")
    _write_report("accountability", status,
                  findings={"agents_checked": len(cards), "breaches": len(breaches),
                            "scorecards": cards,
                            "temporary_workstreams": workstreams,
                            "workstreams_complete": len(workstreams) - len(incomplete),
                            "workstreams_total": len(workstreams)},
                  gaps={"agent_sla": breaches, "monday_workstreams": incomplete}
                  if (breaches or incomplete) else None,
                  duration_ms=int((time.time() - started) * 1000))
