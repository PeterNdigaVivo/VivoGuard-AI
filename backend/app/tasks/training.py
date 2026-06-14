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


@celery_app.task(name="training.weekly_retrain_all", ignore_result=True)
def weekly_retrain_all() -> None:
    """Weekly offline pipeline. For every detection type with a
    feedback pool that's accumulated >=50 new samples since the last
    fine-tune, queues an incremental fine-tune via the existing
    trainer. Each spawned job goes through the standard worker —
    live inference is never blocked because these run on the `beat`
    queue with separate Celery slots."""
    from app.database import SessionLocal
    from app.training.orchestrator import run_weekly_for_all
    with SessionLocal() as db:
        try:
            results = run_weekly_for_all(db)
            log.info("weekly_retrain_all: %s", results)
        except Exception as e:
            log.exception("weekly_retrain_all failed: %s", e)


@celery_app.task(name="training.evaluate_pending_promotions",
                  ignore_result=True)
def evaluate_pending_promotions() -> None:
    """Hourly sweep — picks every AIModel that's NOT currently
    deployed, has a sibling deployed model with the same name, and
    has accumulated enough operator-marked traffic, then promotes
    it if precision + fp_rate beat production. Manual /deploy still
    works for ops overrides."""
    from app.database import SessionLocal
    from app.models import AIModel
    from app.training.promotion import promote
    with SessionLocal() as db:
        try:
            cands = (db.query(AIModel)
                       .filter(AIModel.deployed == False)             # noqa: E712
                       .order_by(AIModel.created_at.desc())
                       .all())
            promoted = 0
            for c in cands:
                r = promote(db, c.id)
                if r.get("promoted"):
                    promoted += 1
            log.info("evaluate_pending_promotions: candidates=%d promoted=%d",
                     len(cands), promoted)
        except Exception as e:
            log.exception("evaluate_pending_promotions failed: %s", e)


@celery_app.task(name="training.pseudo_label_pending", ignore_result=True)
def pseudo_label_pending() -> None:
    """Hourly batch — runs the deployed YOLO over every unlabelled
    TrainingImage in any feedback dataset and writes high-confidence
    detections as auto_suggested+verified Annotations. Anything
    below threshold is left for human review."""
    from app.database import SessionLocal
    from app.training.pseudo_label import pseudo_label_all_pending
    with SessionLocal() as db:
        try:
            r = pseudo_label_all_pending(db)
            log.info("pseudo_label_pending: %s", r)
        except Exception as e:
            log.exception("pseudo_label_pending failed: %s", e)
