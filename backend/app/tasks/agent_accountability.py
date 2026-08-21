"""Five-minute accountability monitor for all registered agents."""
import time

from app.agent_control.accountability import scorecards
from app.database import SessionLocal
from app.tasks.celery_app import celery_app


@celery_app.task(name="agents.accountability", ignore_result=True)
def accountability():
    started = time.time()
    with SessionLocal() as db:
        cards = scorecards(db, persist_cases=True)
        db.commit()
    breaches = [card for card in cards if not card["compliant"]]
    from app.tasks.agents import _write_report
    _write_report("accountability", "critical" if breaches else "ok",
                  findings={"agents_checked": len(cards), "breaches": len(breaches),
                            "scorecards": cards}, gaps=breaches or None,
                  duration_ms=int((time.time() - started) * 1000))
