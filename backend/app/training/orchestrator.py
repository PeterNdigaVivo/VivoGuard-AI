"""Weekly self-learning orchestrator.

Runs offline on the `beat` queue — never touches live inference. The
production worker pool keeps doing real-time detection; only the
single beat slot picks up these tasks once a week (or on manual
trigger). Each detection type is handled independently so a bad
fine-tune for one type can't block the others.

Pipeline per detection type:
  1. Count new positive + negative feedback samples since the last
     fine-tune for this detection type.
  2. Skip when below MIN_NEW_SAMPLES_PER_RETRAIN (default 50).
  3. Pick the parent: most-recently-deployed AIModel whose
     classes_json contains the detection type. Fall back to the
     most-recent model with the same name. Skip if nothing found.
  4. Spawn a fine-tune TrainingJob via the existing pipeline. The
     job auto-unions the positive + hard-negative pools and runs
     under the standard low-LR / short-epoch defaults.
  5. The fine-tune runs in a Celery worker; when it finishes, the
     trained AIModel is registered but NOT deployed. The promotion
     gate in `app.training.promotion` runs in a separate task and
     only flips `deployed` when the candidate beats production on
     precision + fp_rate.

Idempotent — re-running mid-week is safe because step 2 short-circuits
when nothing new has landed.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AIModel, Dataset, TrainingImage, TrainingJob

log = logging.getLogger(__name__)


def _retrain_thresholds() -> tuple[int, int]:
    """Read the auto-retrain thresholds from settings (env-overridable
    via RETRAIN_MIN_FEEDBACK / RETRAIN_MAX_DAYS). Imported lazily so
    pure unit tests of this module can monkey-patch the function
    instead of mutating global state."""
    from app.config import settings
    return (int(getattr(settings, "retrain_min_feedback", 20)),
            int(getattr(settings, "retrain_max_days", 7)))


# Back-compat constant — `weekly_retrain_all` previously imported this.
# Re-resolved to the env-driven value on each access via a module
# attribute so existing callers don't need updating.
MIN_NEW_SAMPLES_PER_RETRAIN = _retrain_thresholds()[0]


def _last_fine_tune_completion(db: Session, detection_type: str) -> datetime | None:
    """When did the last fine-tune FOR THIS detection type complete?
    Used as the "new samples since" cutoff.

    Previously this ignored detection_type and returned the most
    recent incremental fine-tune of ANY type — so a fine-tune of
    'uniform' reset the cutoff for every other type, under-counting
    their new samples and potentially starving a due retrain. The
    job now stamps `detection_type` into config_json (see
    enqueue_fine_tune_if_due) so we can filter on it. Jobs queued
    before that stamp landed return NULL here → since=None → counts
    all samples ever (safe: errs toward firing)."""
    j = (db.query(TrainingJob)
           .filter(TrainingJob.status == "done")
           .filter(TrainingJob.config_json.op("->>")("incremental_finetune") == "true")
           .filter(TrainingJob.config_json.op("->>")("detection_type") == detection_type)
           .order_by(TrainingJob.completed_at.desc())
           .first())
    return j.completed_at if j else None


def _new_samples_since(db: Session, detection_type: str,
                        since: datetime | None) -> tuple[int, int]:
    """Returns (positives_new, negatives_new) created after `since`.
    None → all samples ever."""
    q_pos = (db.query(TrainingImage)
                .join(Dataset, Dataset.id == TrainingImage.dataset_id)
                .filter(Dataset.name == f"feedback-{detection_type}"))
    q_neg = (db.query(TrainingImage)
                .join(Dataset, Dataset.id == TrainingImage.dataset_id)
                .filter(Dataset.name == f"feedback-negative-{detection_type}"))
    if since is not None:
        q_pos = q_pos.filter(TrainingImage.created_at > since)
        q_neg = q_neg.filter(TrainingImage.created_at > since)
    return q_pos.count(), q_neg.count()


def _pick_parent_model(db: Session, detection_type: str) -> AIModel | None:
    """Most-recently-deployed AIModel whose classes_json contains the
    detection type. Falls back to the most-recent deployed chain
    model, then to the most-recent deployed model overall."""
    deployed = (db.query(AIModel)
                  .filter(AIModel.deployed == True)                    # noqa: E712
                  .order_by(AIModel.created_at.desc())
                  .all())
    for m in deployed:
        if detection_type in (m.classes_json or []):
            return m
    for m in deployed:
        if m.is_chain_model:
            return m
    return deployed[0] if deployed else None


def _feedback_pools_exist(db: Session, detection_type: str) -> bool:
    return (db.query(Dataset)
              .filter(Dataset.name == f"feedback-{detection_type}")
              .first()) is not None


def enqueue_fine_tune_if_due(db: Session, detection_type: str,
                              *, min_new: int | None = None,
                              max_days: int | None = None,
                              dry_run: bool = False) -> dict:
    """Decide-and-enqueue for one detection type. Auto-fires when
    EITHER condition holds:
      - ≥ min_new new feedback samples since the last fine-tune
        (default settings.retrain_min_feedback, env RETRAIN_MIN_FEEDBACK)
      - OR ≥ max_days days since the last fine-tune
        (default settings.retrain_max_days, env RETRAIN_MAX_DAYS) —
        the weekly fallback so quiet stores still get periodic
        re-training.

    Returns a status dict with the verdict + counts (does not raise)."""
    cfg_min, cfg_days = _retrain_thresholds()
    if min_new  is None: min_new  = cfg_min
    if max_days is None: max_days = cfg_days

    if not _feedback_pools_exist(db, detection_type):
        return {"detection_type": detection_type,
                "status": "skipped",
                "reason": "no feedback dataset yet"}
    parent = _pick_parent_model(db, detection_type)
    if parent is None:
        return {"detection_type": detection_type,
                "status": "skipped",
                "reason": "no deployed parent model to fine-tune"}
    since = _last_fine_tune_completion(db, detection_type)
    pos_new, neg_new = _new_samples_since(db, detection_type, since)
    days_elapsed = (
        (datetime.now(timezone.utc) - since).total_seconds() / 86400.0
        if since is not None else float("inf")
    )

    enough_samples = (pos_new + neg_new) >= min_new
    overdue        = days_elapsed >= max_days
    if not (enough_samples or overdue):
        return {"detection_type": detection_type,
                "status": "skipped",
                "reason": (f"only {pos_new + neg_new} new samples "
                            f"(need >= {min_new}) and "
                            f"{days_elapsed:.1f}d since last (< {max_days}d)"),
                "positives_new": pos_new, "negatives_new": neg_new,
                "days_since_last": days_elapsed,
                "since": since.isoformat() if since else None}
    trigger = "sample_threshold" if enough_samples else "weekly_fallback"

    if dry_run:
        return {"detection_type": detection_type,
                "status": "would-run",
                "trigger": trigger,
                "parent_model_id": parent.id,
                "positives_new": pos_new, "negatives_new": neg_new,
                "days_since_last": days_elapsed}

    pos_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-{detection_type}").first()
    neg_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-negative-{detection_type}").first()
    cfg = {
        "incremental_finetune":       True,
        "detection_type":             detection_type,   # cutoff filter key
        "resume_from_model_id":       parent.id,
        "extra_negative_dataset_ids": [neg_ds.id] if neg_ds else [],
        "max_neg_ratio":              3.0,
        "epochs":                     10,
        "batch":                      16,
        "imgsz":                      640,
        "lr0":                        0.0005,
        "augment":                    True,
        "origin":                     "weekly_orchestrator",
        "trigger":                    trigger,
    }
    job = TrainingJob(
        model_name=parent.name,
        dataset_id=pos_ds.id,
        config_json=cfg,
        status="queued",
        total_epochs=10,
    )
    db.add(job); db.commit(); db.refresh(job)
    from app.tasks.training import run_training_job
    run_training_job.delay(job.id)
    log.info("orchestrator: queued fine-tune job=%s detection_type=%s "
             "parent=%s pos_new=%d neg_new=%d",
             job.id, detection_type, parent.id, pos_new, neg_new)
    return {"detection_type": detection_type,
            "status": "queued",
            "job_id": job.id,
            "parent_model_id": parent.id,
            "positives_new": pos_new, "negatives_new": neg_new}


def run_weekly_for_all(db: Session, *, dry_run: bool = False) -> list[dict]:
    """Walk every detection_type with a feedback-<type> Dataset.
    Returns one status dict per type."""
    pools = (db.query(Dataset)
                .filter(Dataset.name.like("feedback-%"))
                .filter(~Dataset.name.like("feedback-negative-%"))
                .all())
    types = sorted({d.name[len("feedback-"):] for d in pools})
    return [enqueue_fine_tune_if_due(db, t, dry_run=dry_run) for t in types]
