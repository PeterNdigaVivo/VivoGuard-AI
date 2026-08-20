"""Training-job + drift-metrics Celery tasks."""
from __future__ import annotations
import logging

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="training.write_preview_for_image", ignore_result=True)
def write_preview_for_image(image_id: int) -> None:
    """Generate the orange-box review preview for a TrainingImage. Runs on
    the WORKER (which has opencv) because the API container intentionally
    ships without it — the reason previews were never generated. Best-effort:
    a failure never affects the already-saved training image."""
    from app.database import SessionLocal
    from app.models import TrainingImage
    from app.training.image_preview import write_preview, label_from_uniform_color
    try:
        with SessionLocal() as db:
            img = db.get(TrainingImage, image_id)
            if not img or not img.file_path or img.preview_path:
                return
            uc = None
            se = img.source_extra if isinstance(img.source_extra, dict) else {}
            uc = (se or {}).get("uniform_color")
            preview = write_preview(img.file_path,
                                    label=label_from_uniform_color(uc),
                                    bbox_norm=None)
            if preview:
                img.preview_path = preview
                db.commit()
                log.info("preview: set training_image=%s -> %s", image_id, preview)
            else:
                log.warning("preview: write returned None for training_image=%s "
                            "path=%s", image_id, img.file_path)
    except Exception:
        log.exception("write_preview_for_image failed image=%s", image_id)


@celery_app.task(name="training.backfill_previews", ignore_result=True)
def backfill_previews(limit: int = 500) -> None:
    """One-shot (idempotent) backfill of orange-box previews for existing
    TrainingImage rows whose preview_path is NULL — the rows created before
    the API-container/opencv fix. Self-limiting: processes up to `limit` NULL
    rows per run and becomes a no-op once every row has a preview. Scheduled
    hourly so it backfills automatically after a deploy, then costs nothing.
    Runs on the worker (opencv present)."""
    from app.database import SessionLocal
    from app.models import TrainingImage
    from app.training.image_preview import write_preview, label_from_uniform_color
    done = 0
    with SessionLocal() as db:
        rows = (db.query(TrainingImage)
                  .filter(TrainingImage.preview_path.is_(None),
                          TrainingImage.file_path.isnot(None))
                  .limit(limit).all())
        for img in rows:
            se = img.source_extra if isinstance(img.source_extra, dict) else {}
            uc = (se or {}).get("uniform_color")
            try:
                preview = write_preview(img.file_path,
                                        label=label_from_uniform_color(uc),
                                        bbox_norm=None)
            except Exception:
                preview = None
            if preview:
                img.preview_path = preview
                done += 1
        if done:
            db.commit()
    if rows:
        log.info("backfill_previews: generated %d/%d previews (limit=%d, "
                 "%d remaining candidates this pass)", done, len(rows), limit,
                 max(0, len(rows) - done))


@celery_app.task(name="training.run_job", bind=True, ignore_result=True)
def run_training_job(self, job_id: int) -> None:
    """Celery entry point for one training job.

    Synchronous debug trigger (runs inline in the current process,
    bypassing the broker entirely):

        docker compose exec -T worker-alerts python3 -c "
        from app.tasks.training import run_training_job
        run_training_job.apply(args=[734])
        "
    """
    from app.training.trainer import run_job
    log.info("job %s: celery task received (task_id=%s) — handing to "
             "trainer", job_id, getattr(getattr(self, "request", None),
                                        "id", None))
    # Concurrency cap at EXECUTION time. The dispatcher already caps
    # its own dispatches at 2, but every creation-time .delay() path
    # (cross-store button, specialists, orchestrator triggers) lands
    # here directly — that's how jobs 608/609/611 trained
    # simultaneously and filled the whole alerts pool. If 2 jobs are
    # actively training (heartbeat stamped), bounce this one back to
    # queued; the dispatcher redispatches when a slot frees.
    try:
        from app.database import SessionLocal
        from app.models import TrainingJob
        with SessionLocal() as db:
            active = (db.query(TrainingJob)
                        .filter(TrainingJob.status == "running",
                                TrainingJob.id != job_id,
                                TrainingJob.last_progress_at.isnot(None))
                        .count())
            if active >= 2:
                j = db.get(TrainingJob, job_id)
                if j is not None and j.status in ("queued", "running"):
                    old = j.status
                    j.status = "queued"
                    j.started_at = None
                    j.celery_task_id = None
                    db.commit()
                    log.info("job %s: state %s -> queued (concurrency "
                             "cap: %d jobs already training)",
                             job_id, old, active)
                return
    except Exception:
        log.exception("job %s: concurrency-cap check failed — "
                      "proceeding anyway", job_id)
    try:
        run_job(job_id)
    except Exception as e:
        log.exception("training job %s failed: %s", job_id, e)
        raise


# Stale `running` jobs older than this many hours are flipped to
# `failed` so the dispatcher can keep moving. We DO NOT auto-retry —
# the spec is explicit that failed training requires human review.
# (The stall watchdog below usually fires long before this backstop.)
_STALE_RUNNING_HOURS = 12
# Stall watchdog: a running job whose progress heartbeat
# (last_progress_at, bumped per epoch by the trainer) is older than
# training_stall_timeout_minutes gets its Celery task revoked and the
# job requeued — at most this many times, then it fails for review.
# Bounded so a deterministic crash can't requeue-loop forever.
_MAX_STALL_REQUEUES = 2
# Cap per-tick dispatches so a 100-job backlog doesn't slam the
# worker-alerts pool all at once.
_DISPATCH_MAX_PER_TICK = 3
# Circuit breaker — when a dataset has accumulated this many failed
# TrainingJobs (all-time, not consecutive), the next dispatch is
# short-circuited to `failed` and the Dataset.description is stamped
# with a `[suspended]` marker. Operators clear the marker to re-enable.
_MAX_FAILURES_PER_DATASET = 3
_SUSPENDED_MARKER = "[suspended]"


def _requeue_or_fail(db, j, reason: str) -> None:
    """Shared recovery move for a wedged `running` job: revoke its
    Celery task (also blocks acks_late redelivery from double-running
    it), then requeue — bounded by _MAX_STALL_REQUEUES via the
    config_json.stall_requeues counter, after which the job fails for
    operator review. Caller commits. Every transition is logged with
    the job id + epoch position."""
    from datetime import datetime as _dt, timezone as _tz
    if j.celery_task_id:
        try:
            celery_app.control.revoke(j.celery_task_id, terminate=True)
        except Exception as e:
            log.warning("job %s: revoke of task %s failed: %s",
                        j.id, j.celery_task_id, e)
    cfg = dict(j.config_json or {})
    n = int(cfg.get("stall_requeues", 0)) + 1
    old = j.status
    if n > _MAX_STALL_REQUEUES:
        j.status = "failed"
        j.completed_at = _dt.now(_tz.utc)
        j.error_message = (
            f"auto-failed: {reason}; already requeued "
            f"{_MAX_STALL_REQUEUES} time(s). Review manually.")
        log.error("job %s: state %s -> failed (%s) at epoch %s/%s",
                  j.id, old, reason, j.current_epoch, j.total_epochs)
    else:
        cfg["stall_requeues"] = n
        j.config_json = cfg
        j.status = "queued"
        j.started_at = None
        j.last_progress_at = None
        j.celery_task_id = None
        j.error_message = (
            f"auto-requeued ({n}/{_MAX_STALL_REQUEUES}): {reason}")
        log.warning("job %s: state %s -> queued (%s) attempt %d/%d "
                    "at epoch %s/%s", j.id, old, reason, n,
                    _MAX_STALL_REQUEUES, j.current_epoch, j.total_epochs)


def reset_zombie_running_jobs() -> int:
    """Worker-restart recovery: training runs ONLY on the alerts-queue
    worker, so when that worker (re)starts, every TrainingJob still
    marked `running` is a zombie whose process died with the previous
    worker. Requeue them all (same bounded counter — an OOM-crash loop
    can't retrain forever). Returns how many were reset."""
    from app.database import SessionLocal
    from app.models import TrainingJob
    with SessionLocal() as db:
        zombies = (db.query(TrainingJob)
                     .filter(TrainingJob.status == "running").all())
        for j in zombies:
            _requeue_or_fail(db, j, "worker restarted while job was running")
        if zombies:
            db.commit()
            log.warning("worker start: reset %d zombie running job(s): %s",
                        len(zombies), [j.id for j in zombies])
        return len(zombies)


try:
    from celery.signals import worker_ready

    @worker_ready.connect
    def _on_worker_ready(sender=None, **_kw) -> None:
        """Run the zombie reset only on the worker that actually
        consumes the `alerts` queue (where training runs). If queue
        introspection fails we skip — the dispatcher watchdog still
        recovers within one tick, this is just the fast path."""
        try:
            qnames = {q.name for q in sender.task_consumer.queues}
        except Exception:
            qnames = set()
        if "alerts" not in qnames:
            return
        try:
            reset_zombie_running_jobs()
        except Exception:
            log.exception("zombie-job reset failed at worker start")
except Exception:                                   # pragma: no cover
    log.exception("could not register worker_ready zombie reset")


@celery_app.task(name="training.dispatch_queued_jobs", ignore_result=True)
def dispatch_queued_jobs() -> None:
    """The missing link between "TrainingJob created" and "trainer
    actually runs".

    Every 5 min:
      1. Flip any stale `running` jobs (> _STALE_RUNNING_HOURS old)
         to `failed` with a diagnostic error_message. No auto-retry
         per spec — operators must inspect.
      2. Pick up to _DISPATCH_MAX_PER_TICK oldest `queued` jobs,
         atomically mark them `running`, and enqueue
         `training.run_job` on the `alerts` queue (NOT the inference
         queue — live detection must never be interrupted by training).

    The original `.delay()` at job-creation time still fires
    immediately on the happy path; this task is the recovery net for
    the (real) cases where it doesn't — worker restarts, broker
    blips, the existing job#1 sitting queued before this code shipped,
    etc.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import func as _func
    from app.config import settings
    from app.database import SessionLocal
    from app.models import TrainingJob

    with SessionLocal() as db:
        # 0) Two-tier watchdog over `running` jobs.
        #    Tier A (fast, "never started"): last_progress_at is NULL —
        #      stamping it is the trainer's FIRST act, so NULL means the
        #      worker never picked the task up (the job-734 pattern:
        #      running for 10+ min, no heartbeat, no epochs). Requeued
        #      after training_start_timeout_seconds (default 60s).
        #    Tier B (slow, "stalled mid-train"): heartbeat exists but is
        #      older than training_stall_timeout_minutes (default 90).
        #    Both revoke the Celery task first (also blocks an acks_late
        #    redelivery from double-running) and are bounded by the
        #    stall_requeues counter → failed for review after 2 requeues.
        start_s   = int(getattr(settings, "training_start_timeout_seconds", 60))
        stall_min = int(getattr(settings, "training_stall_timeout_minutes", 90))
        now_utc = _dt.now(_tz.utc)
        swept = 0
        for j in (db.query(TrainingJob)
                    .filter(TrainingJob.status == "running").all()):
            hb = j.last_progress_at or j.started_at or j.created_at
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=_tz.utc)
            age_s = ((now_utc - hb).total_seconds()
                     if hb is not None else float("inf"))
            if j.last_progress_at is None:
                if start_s <= 0 or age_s < start_s:
                    continue
                reason = (f"trainer never started within {start_s}s "
                          f"(task not picked up by the worker)")
            else:
                if stall_min <= 0 or age_s < stall_min * 60:
                    continue
                reason = f"no epoch progress for > {stall_min} min"
            _requeue_or_fail(db, j, reason)
            swept += 1
        if swept:
            db.commit()

        # 1) Sweep stale running jobs. coalesce() so a running row with
        # started_at=NULL (crash between status flip and timestamp
        # write) can't dodge the sweep and hold a cap slot forever.
        cutoff = _dt.now(_tz.utc) - _td(hours=_STALE_RUNNING_HOURS)
        stale = (db.query(TrainingJob)
                   .filter(TrainingJob.status == "running",
                           _func.coalesce(TrainingJob.started_at,
                                          TrainingJob.created_at) < cutoff)
                   .all())
        for j in stale:
            j.status = "failed"
            j.error_message = (
                f"auto-failed by dispatcher: status=running for > "
                f"{_STALE_RUNNING_HOURS}h since {j.started_at!r}. "
                f"Worker likely crashed. Review and re-queue manually.")
            j.completed_at = _dt.now(_tz.utc)
            log.warning("dispatcher: stale job %s flipped to failed", j.id)
        if stale:
            db.commit()

        # Parallel-job lock (Part 2 #5, Q3): at most 2 running jobs at once,
        # and never 2 on the same detection_type. Simple DB status count —
        # no locks, no deadlock risk. worker-alerts has 4 slots so 2 training
        # jobs still leave room for alert tasks.
        running = (db.query(TrainingJob)
                     .filter(TrainingJob.status == "running").all())
        if len(running) >= 2:
            log.info("dispatcher: %d job(s) already running (cap 2) — "
                     "skipping this tick", len(running))
            return
        running_types = {(j.config_json or {}).get("detection_type")
                         for j in running}

        # 2) Pick queued jobs by PRIORITY (lower = sooner) then FIFO
        # (Part 2 #6), mark running, dispatch.
        picks = (db.query(TrainingJob)
                   .filter(TrainingJob.status == "queued")
                   .order_by(TrainingJob.priority.asc(),
                             TrainingJob.created_at.asc())
                   .limit(_DISPATCH_MAX_PER_TICK)
                   .all())
        dispatched: list[int] = []
        suspended: list[int] = []
        for j in picks:
            if len(running) >= 2:
                break
            dtype = (j.config_json or {}).get("detection_type")
            if dtype and dtype in running_types:
                continue    # never run 2 jobs on the same detection_type
            # Defense in depth — the query above already filters
            # status='queued', but in case anything flips a failed
            # job back to queued by hand (or a SQLAlchemy session
            # cache returns a stale row), re-check here. Failed jobs
            # NEVER auto-retry through this path.
            if j.status != "queued":
                continue

            # Circuit breaker — count all-time failures on this
            # dataset. Once we hit the threshold, this job and any
            # future queued job for the same dataset short-circuit
            # to `failed` until an operator clears the `[suspended]`
            # marker from Dataset.description.
            from app.models import Dataset as _Dataset
            ds = db.get(_Dataset, j.dataset_id) if j.dataset_id else None
            already_suspended = bool(
                ds and (ds.description or "").startswith(_SUSPENDED_MARKER)
            )
            failures = (db.query(TrainingJob)
                          .filter(TrainingJob.dataset_id == j.dataset_id,
                                  TrainingJob.status == "failed")
                          .count())
            if already_suspended or failures >= _MAX_FAILURES_PER_DATASET:
                if ds is not None and not already_suspended:
                    prior = ds.description or "(none)"
                    ds.description = (
                        f"{_SUSPENDED_MARKER} {failures} training failures — "
                        f"operator review required. Prior description: {prior}"
                    )
                j.status = "failed"
                j.error_message = (
                    f"Dataset has {failures} prior failures "
                    f"(>= {_MAX_FAILURES_PER_DATASET}) — auto-suspended. "
                    f"Clear the {_SUSPENDED_MARKER} prefix from the dataset "
                    f"description to re-enable training."
                )
                j.completed_at = _dt.now(_tz.utc)
                db.commit()
                suspended.append(j.id)
                log.error("dispatcher: dataset=%s suspended after %d failures; "
                          "job=%s short-circuited to failed",
                          j.dataset_id, failures, j.id)
                continue

            # Flip status BEFORE .apply_async so a re-entrant tick
            # never picks the same job twice. The trainer also sets
            # `status='running'` at the top of run_job — both writes
            # are idempotent.
            j.status     = "running"
            j.started_at = j.started_at or _dt.now(_tz.utc)
            db.commit()
            log.info("job %s: state queued -> running (dispatcher) "
                     "at epoch %s/%s", j.id, j.current_epoch, j.total_epochs)
            try:
                res = run_training_job.apply_async(args=[j.id], queue="alerts")
                # Task id lets the stall watchdog revoke a stuck run.
                j.celery_task_id = getattr(res, "id", None)
                db.commit()
                dispatched.append(j.id)
                running.append(j)               # count toward the cap of 2
                if dtype:
                    running_types.add(dtype)     # block same-type this tick
            except Exception as e:
                # apply_async failed (broker down?). Roll the job
                # back to queued so the next tick retries.
                j.status = "queued"
                j.started_at = None
                j.celery_task_id = None
                db.commit()
                log.exception("dispatcher: apply_async failed for job=%s: %s",
                              j.id, e)
        if suspended:
            log.warning("dispatcher: %d job(s) short-circuited via circuit "
                        "breaker: %s", len(suspended), suspended)
        if dispatched:
            log.info("dispatcher: dispatched %d jobs %s",
                     len(dispatched), dispatched)


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
