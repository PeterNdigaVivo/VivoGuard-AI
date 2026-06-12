"""Training-job + drift-metrics Celery tasks."""
from __future__ import annotations
import logging

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="training.run_job", bind=True, ignore_result=True)
def run_training_job(self, job_id: int) -> None:
    from app.training.trainer import run_job
    log.info("run_training_job id=%s", job_id)
    try:
        run_job(job_id)
    except Exception as e:
        log.exception("training job %s failed: %s", job_id, e)
        raise


@celery_app.task(name="training.compute_model_metrics_daily",
                  ignore_result=True)
def compute_model_metrics_daily() -> None:
    """Refresh the drift dashboard. Each tick recomputes the last 2
    EAT days so late operator confirms/dismisses are picked up. The
    upsert is keyed on (model_id, detection_type, day) so re-running
    is safe."""
    from app.database import SessionLocal
    from app.training.metrics import backfill_recent_days

    with SessionLocal() as db:
        try:
            backfill_recent_days(db, days=2)
        except Exception as e:
            log.exception("compute_model_metrics_daily failed: %s", e)
