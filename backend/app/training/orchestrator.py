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

# YOLO fine-tune floor. Below this many positive images, validation
# metrics are essentially random and the gradient updates overfit on
# the tiny set — worse-than-useless training run. We bail BEFORE
# creating a TrainingJob row so the dispatcher / circuit breaker
# don't have to deal with predictable failures.
# Adaptive minimum positive-image floor per detection type (Part 2 #1) —
# small, high-value detectors train sooner than the generic 30 floor.
MIN_IMAGES = {
    "uniform_compliance": 15,
    "shop_open_close":    20,
    "checkout_dwell":     20,
    "staff_present":      20,
    "default":            30,
}


def _min_images(detection_type: str) -> int:
    return MIN_IMAGES.get(detection_type, MIN_IMAGES["default"])


# Training-dispatch priority per detection type (lower = sooner). Part 2 #6.
# Aug-2026 feedback mission: uniform > shop_open_close > intrusion > others.
TRAIN_PRIORITY = {
    "uniform_compliance": 1,
    "shop_open_close":    2,
    "intrusion":          3,
    "checkout_dwell":     4,
    "staff_present":      4,
}


def _priority_for(detection_type: str) -> int:
    return TRAIN_PRIORITY.get(detection_type, 5)


MIN_DATASET_IMAGES = 30   # legacy default (per-type floors above supersede)

# Extra datasets folded into a detection type's fine-tune ON TOP of the
# operator-feedback pools (Part 6 follow-up). Mined crops live in SEPARATE
# datasets from operator feedback so quality can be tracked independently,
# but they train together. write_yolo_dataset_yaml buckets each image as
# positive/negative by annotation presence, so listing extra positive AND
# negative datasets here works regardless of role.
_EXTRA_POS_DATASETS: dict[str, list[str]] = {
    "uniform_compliance": ["vivo_staff_uniform_v1"],
}
_EXTRA_NEG_DATASETS: dict[str, list[str]] = {
    "uniform_compliance": ["feedback-negative-uniform"],
}


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
                .filter(Dataset.name == f"feedback-{detection_type}",
                        TrainingImage.eligible_for_training.is_(True),
                        TrainingImage.review_state == "approved"))
    q_neg = (db.query(TrainingImage)
                .join(Dataset, Dataset.id == TrainingImage.dataset_id)
                .filter(Dataset.name == f"feedback-negative-{detection_type}",
                        TrainingImage.eligible_for_training.is_(True),
                        TrainingImage.review_state == "approved"))
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
    pos_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-{detection_type}").first()
    from app.training.circuit_breaker import dataset_circuit_state
    circuit = dataset_circuit_state(db, pos_ds)
    if circuit["suspended"]:
        return {"detection_type": detection_type,
                "status": "suspended",
                "reason": circuit["reason"],
                "dataset_id": pos_ds.id if pos_ds else None,
                "dataset_failures": circuit["failures"]}
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

    neg_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-negative-{detection_type}").first()

    # Extra datasets folded in on top of the feedback pools (mined crops etc.).
    extra_pos_ds = [d for d in (db.query(Dataset).filter(Dataset.name == n).first()
                                for n in _EXTRA_POS_DATASETS.get(detection_type, []))
                    if d is not None]
    extra_neg_ds = [d for d in (db.query(Dataset).filter(Dataset.name == n).first()
                                for n in _EXTRA_NEG_DATASETS.get(detection_type, []))
                    if d is not None]

    # Minimum-size guard. Counts POSITIVES across the feedback pool AND any
    # extra positive datasets (e.g. mined vivo_staff_uniform_v1) — a 1-positive
    # set would otherwise train on noise and crash on validation.
    pos_dataset_ids = ([pos_ds.id] if pos_ds is not None else []) + \
                      [d.id for d in extra_pos_ds]
    pos_count = 0
    if pos_dataset_ids:
        pos_count = (db.query(TrainingImage)
                       .filter(TrainingImage.dataset_id.in_(pos_dataset_ids),
                               TrainingImage.eligible_for_training.is_(True),
                               TrainingImage.review_state == "approved").count())
        _floor = _min_images(detection_type)
        if pos_count < _floor:
            log.warning(
                "orchestrator: dataset too small for training "
                "(%d images, need %d+) — detection_type=%s datasets=%s",
                pos_count, _floor, detection_type, pos_dataset_ids)
            return {"detection_type": detection_type,
                    "status": "skipped",
                    "reason": (f"dataset too small ({pos_count} images, "
                                f"need {_floor}+)"),
                    "positives_total": pos_count}
    # All extra datasets (positive + negative) ride in extra_negative_dataset_ids;
    # write_yolo_dataset_yaml classifies each image by annotation presence, so
    # extra positives (with annotations) train as positives.
    extra_ids = [d.id for d in extra_pos_ds]
    if neg_ds is not None:
        extra_ids.append(neg_ds.id)
    extra_ids += [d.id for d in extra_neg_ds]

    # Anti-catastrophic-forgetting replay: sample ~18% of the parent
    # model's ORIGINAL training dataset into this fine-tune (train split
    # only — see write_yolo_dataset_yaml). Resolved here because only the
    # orchestrator knows the parent; the trainer just reads the cfg key.
    base_mix_dataset_id: int | None = None
    if parent.training_job_id:
        parent_job = db.get(TrainingJob, parent.training_job_id)
        if (parent_job and parent_job.dataset_id
                and pos_ds is not None
                and parent_job.dataset_id != pos_ds.id):
            base_mix_dataset_id = parent_job.dataset_id

    # Combined-size projection — don't enqueue a job the trainer's
    # InsufficientDataError gate (settings.min_training_images, default 50)
    # is guaranteed to abort. Projection mirrors the trainer's real
    # composition: positives + capped negatives + replay mix.
    from app.config import settings as _settings
    _min_total = int(getattr(_settings, "min_training_images", 50))
    neg_ids = ([neg_ds.id] if neg_ds is not None else []) + \
              [d.id for d in extra_neg_ds]
    neg_count = (db.query(TrainingImage)
                   .filter(TrainingImage.dataset_id.in_(neg_ids),
                           TrainingImage.eligible_for_training.is_(True),
                           TrainingImage.review_state == "approved").count()
                 if neg_ids else 0)
    _mix_frac = float(getattr(_settings, "base_mix_fraction", 0.18))
    projected = (pos_count + min(neg_count, int(3.0 * pos_count))
                 + (int(pos_count * _mix_frac) if base_mix_dataset_id else 0))
    if projected < _min_total:
        log.warning(
            "orchestrator: projected combined dataset too small for training "
            "(%d < %d) — detection_type=%s pos=%d neg=%d mix=%s",
            projected, _min_total, detection_type, pos_count, neg_count,
            base_mix_dataset_id)
        return {"detection_type": detection_type,
                "status": "skipped",
                "reason": (f"projected combined dataset {projected} images "
                           f"< min_training_images {_min_total} "
                           f"(pos={pos_count} neg={neg_count})"),
                "positives_total": pos_count, "negatives_total": neg_count}

    cfg = {
        "incremental_finetune":       True,
        "detection_type":             detection_type,   # cutoff filter key
        "resume_from_model_id":       parent.id,
        "extra_negative_dataset_ids": extra_ids,
        "base_mix_dataset_id":        base_mix_dataset_id,
        "max_neg_ratio":              3.0,
        # epochs intentionally omitted — the trainer sizes it to the dataset
        # (Part 2 #2): <50 img -> 20, <200 -> 15, else 10.
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
        priority=_priority_for(detection_type),
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


def has_open_job(db: Session, detection_type: str) -> bool:
    """True when a queued/running TrainingJob already exists for this
    detection type — the aggressive click-triggered path must not pile
    duplicate jobs onto the queue between clicks 10..N."""
    open_jobs = (db.query(TrainingJob)
                   .filter(TrainingJob.status.in_(("queued", "running")))
                   .all())
    return any((j.config_json or {}).get("detection_type") == detection_type
               for j in open_jobs)


def enqueue_full_retrain(db: Session, detection_type: str) -> dict:
    """FULL retrain (from base weights, not incremental) for one
    detection type — the 30-click escalation of the aggressive
    feedback schedule. Reuses enqueue_fine_tune_if_due's dataset
    plumbing by building the same job shape with
    incremental_finetune=False; the trainer then starts from
    base_model instead of the parent weights."""
    if not _feedback_pools_exist(db, detection_type):
        return {"detection_type": detection_type, "status": "skipped",
                "reason": "no feedback dataset yet"}
    pos_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-{detection_type}").first()
    neg_ds = db.query(Dataset).filter(
        Dataset.name == f"feedback-negative-{detection_type}").first()
    if pos_ds is None:
        return {"detection_type": detection_type, "status": "skipped",
                "reason": "no positive pool"}
    from app.training.circuit_breaker import dataset_circuit_state
    circuit = dataset_circuit_state(db, pos_ds)
    if circuit["suspended"]:
        return {"detection_type": detection_type, "status": "suspended",
                "reason": circuit["reason"], "dataset_id": pos_ds.id,
                "dataset_failures": circuit["failures"]}
    pos_count = (db.query(TrainingImage)
                   .filter(TrainingImage.dataset_id == pos_ds.id,
                           TrainingImage.eligible_for_training.is_(True),
                           TrainingImage.review_state == "approved").count())
    if pos_count < _min_images(detection_type):
        return {"detection_type": detection_type, "status": "skipped",
                "reason": f"only {pos_count} positives "
                          f"(need {_min_images(detection_type)}+)"}
    parent = _pick_parent_model(db, detection_type)
    cfg = {
        "incremental_finetune":       False,
        "detection_type":             detection_type,
        "extra_negative_dataset_ids": ([neg_ds.id] if neg_ds else []),
        "max_neg_ratio":              3.0,
        "batch":                      16,
        "imgsz":                      640,
        "augment":                    True,
        "origin":                     "feedback_full_retrain",
    }
    job = TrainingJob(
        model_name=(parent.name if parent else f"vivo_{detection_type}"),
        dataset_id=pos_ds.id,
        config_json=cfg,
        status="queued",
        priority=_priority_for(detection_type),
    )
    db.add(job); db.commit(); db.refresh(job)
    from app.tasks.training import run_training_job
    run_training_job.delay(job.id)
    log.info("orchestrator: queued FULL retrain job=%s detection_type=%s "
             "positives=%d", job.id, detection_type, pos_count)
    return {"detection_type": detection_type, "status": "queued",
            "job_id": job.id, "mode": "full_retrain",
            "positives_total": pos_count}


def run_weekly_for_all(db: Session, *, dry_run: bool = False) -> list[dict]:
    """Walk every detection_type with a feedback-<type> Dataset.
    Returns one status dict per type."""
    pools = (db.query(Dataset)
                .filter(Dataset.name.like("feedback-%"))
                .filter(~Dataset.name.like("feedback-negative-%"))
                .all())
    types = sorted({d.name[len("feedback-"):] for d in pools})
    return [enqueue_fine_tune_if_due(db, t, dry_run=dry_run) for t in types]


# ── Cross-store generalist dataset (top-3 stores) ──────────────────────────
# Only these stores have proven-accurate operator feedback. Edit if the DB
# store names differ (the JOIN matches Store.name exactly).
# Vivo Garden City removed Aug 2026 — 0 training images contributed.
CROSS_STORE_STORES = ["Vivo Junction", "Vivo Runda", "Vivo Yaya"]
CROSS_STORE_DATASET = "vivo_cross_store_v1"
CROSS_STORE_MIN_IMAGES = 30


def _cross_store_query(db: Session):
    """Confirmed TrainingImages from the CROSS_STORE_STORES list, newest
    first, joined to their DetectionEvent (for the class label) + Store
    name."""
    from app.models import Alert, Camera, Store
    from app.models import DetectionEvent  # noqa: F811
    return (db.query(TrainingImage, DetectionEvent, Store.name)
              .join(Alert, Alert.id == TrainingImage.source_alert_id)
              .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
              .join(Camera, Camera.id == DetectionEvent.camera_id)
              .join(Store, Store.id == Camera.store_id)
              .filter(Store.name.in_(CROSS_STORE_STORES),
                      Alert.feedback_used_for_training.is_(True),
                      TrainingImage.file_path.isnot(None))
              .order_by(TrainingImage.created_at.desc())
              .all())


def build_cross_store_dataset(db: Session) -> dict:
    """(Re)build the vivo_cross_store_v1 dataset from confirmed images across
    the CROSS_STORE_STORES list. Soft-prefers images with an orange-box
    preview and a real file (>50KB); falls back to all confirmed if that
    drops below 30. Returns {store_counts, total_images, dataset_id}."""
    import os
    from app.training.dataset import split_dataset
    from app.training.feedback_loop import _ensure_dataset

    rows = _cross_store_query(db)
    if not rows:
        return {"store_counts": {}, "total_images": 0, "dataset_id": None,
                "reason": "no confirmed images from the top-3 stores "
                          "(check store names match the DB)"}

    def _quality(ti) -> bool:
        try:
            return bool(ti.preview_path and ti.file_path
                        and os.path.exists(ti.file_path)
                        and os.path.getsize(ti.file_path) > 50 * 1024)
        except Exception:
            return False

    filtered = [r for r in rows if _quality(r[0])]
    use = filtered if len(filtered) >= CROSS_STORE_MIN_IMAGES else rows
    log.info("cross_store: %d confirmed images (%d passed quality filter) — "
             "using %d", len(rows), len(filtered), len(use))

    classes = sorted({ev.detection_type for (_ti, ev, _s) in use if ev.detection_type})
    ds = _ensure_dataset(db, CROSS_STORE_DATASET, classes,
                         description="auto: cross-store generalist (top-3 stores)")
    # Rebuild fresh each run so counts reflect the current confirmed
    # pool. ATOMIC (Aug 2026): delete + re-add commit TOGETHER — the
    # old commit-right-after-delete left the dataset permanently EMPTY
    # whenever the re-add was interrupted, and every queued job then
    # failed at prep with "insufficient validation images: 0 < 5".
    db.query(TrainingImage).filter(TrainingImage.dataset_id == ds.id).delete()

    from app.models import Annotation
    from app.training.feedback_loop import _training_provenance
    # Clones derive from operator-CONFIRMED alerts; stamp provenance
    # explicitly (honours training_require_dual_review) instead of
    # relying on column defaults — migration 0042 quarantined the
    # default-stamped clones (source_alert_id IS NULL) wholesale.
    prov = _training_provenance("correct")
    store_counts: dict[str, int] = {}
    for (ti, ev, sname) in use:
        # The training image is ALREADY a clean person crop (the alert
        # thumbnail); the source frame isn't persisted, so sv.crop_image
        # re-extraction is N/A — reference the existing crop directly.
        new = TrainingImage(
            dataset_id=ds.id, camera_id=ti.camera_id,
            file_path=ti.file_path, labeled=True,
            source_extra={"source": "cross_store", "origin_store": sname,
                          "origin_image_id": ti.id,
                          "detection_type": ev.detection_type},
            **prov,
        )
        db.add(new); db.flush()
        if ev.detection_type:
            db.add(Annotation(image_id=new.id, class_label=ev.detection_type,
                              bbox_json=[0.5, 0.5, 1.0, 1.0], verified=True))
        store_counts[sname] = store_counts.get(sname, 0) + 1
    db.commit()   # single commit — delete + re-add are all-or-nothing

    split_dataset(db, ds.id, train=0.8, val=0.2, test=0.0)
    total = sum(store_counts.values())
    log.info("cross_store: dataset=%s rebuilt total=%d per-store=%s",
             ds.id, total, store_counts)
    return {"store_counts": store_counts, "total_images": total, "dataset_id": ds.id}
