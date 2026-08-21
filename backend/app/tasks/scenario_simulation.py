"""Scheduled isolated scenario agent."""
import time

from app.simulation.runner import run_catalog
from app.tasks.celery_app import celery_app


@celery_app.task(name="agents.scenario_simulator", ignore_result=True)
def scenario_simulator():
    started = time.time()
    result = run_catalog()
    from app.tasks.agents import _heartbeat, _redis, _write_report
    _heartbeat(_redis(), "scenario_simulator")
    _write_report("scenario_simulator", "ok" if result["failed"] == 0 else "critical",
                  findings=result,
                  gaps=[r for r in result["results"] if not r["passed"]] or None,
                  duration_ms=int((time.time() - started) * 1000))
    return result
