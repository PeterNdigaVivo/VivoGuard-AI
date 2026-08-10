"""Training job runner.

Invoked from `app.tasks.training.run_training_job`. Steps:
  1. Load TrainingJob row, mark running
  2. Split dataset (writes `split` on each TrainingImage)
  3. Write data.yaml + YOLO label .txt files
  4. Run ultralytics YOLO.train with a callback that publishes per-epoch
     metrics to Redis and updates the job row
  5. On success, persist a new AIModel row pointing at the best.pt weights
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import redis

from app.config import settings
from app.database import SessionLocal
from app.models import AIModel, Dataset, TrainingJob
from app.training.dataset import dataset_root, split_dataset, write_yolo_dataset_yaml
from app.training.sanitize import sanitize_dataset

log = logging.getLogger(__name__)

# Aggressive augmentation defaults (Part 2 #3, Q7) — multiplies effective
# dataset size ~4-8x; critical for lighting variance across 28 stores.
# A job may override/extend via cfg["augmentation"]; the merged dict is
# validated against the installed ultralytics' config keys BEFORE training
# so an invalid key fails fast instead of surfacing mid-run.
DEFAULT_AUGMENTATION: dict[str, float] = {
    "degrees": 10.0, "translate": 0.1, "scale": 0.5, "fliplr": 0.5,
    "mosaic": 0.5, "hsv_h": 0.015, "hsv_s": 0.7, "hsv_v": 0.4,
}


def _ultralytics_cfg_keys() -> set[str]:
    """Authoritative set of train-config keys for the INSTALLED ultralytics.
    (`YOLO.train` accepts **kwargs, so signature introspection is useless —
    the default-config dict is the real schema.)"""
    from ultralytics.cfg import DEFAULT_CFG_DICT
    return set(DEFAULT_CFG_DICT.keys())


def validate_augmentation_config(aug: dict[str, object], *,
                                 valid_keys: set[str] | None = None) -> None:
    """Fail-fast validation of augmentation kwargs. Raises ValueError naming
    every unknown key / non-numeric value. Replaces the old silent
    TypeError-retry (which dropped the whole augmentation block mid-run)."""
    if valid_keys is None:
        valid_keys = _ultralytics_cfg_keys()
    unknown = sorted(set(aug) - valid_keys)
    if unknown:
        raise ValueError(
            f"augmentation config contains keys the installed ultralytics "
            f"does not support: {unknown} — fix the job's cfg['augmentation']")
    non_numeric = sorted(k for k, v in aug.items()
                         if not isinstance(v, (int, float)) or isinstance(v, bool))
    if non_numeric:
        raise ValueError(
            f"augmentation config values must be numeric: {non_numeric}")


def _deployed_parent_for(detection_type: str | None):
    """(weights_path, model_id) of the latest DEPLOYED model whose classes
    include `detection_type`, when its weights file exists on disk — else
    (None, None). Used so each fine-tune BUILDS on the last deployed model
    (Part 2 #7 incremental fine-tuning)."""
    if not detection_type:
        return None, None
    from app.models import AIModel
    try:
        with SessionLocal() as db:
            models = (db.query(AIModel)
                        .filter(AIModel.deployed == True)              # noqa: E712
                        .order_by(AIModel.id.desc()).all())
        for m in models:
            if (detection_type in (m.classes_json or [])
                    and m.weights_path and os.path.exists(m.weights_path)):
                return m.weights_path, m.id
    except Exception as e:
        log.warning("trainer: deployed-parent lookup failed for %s: %s",
                    detection_type, e)
    return None, None


def _publish(channel: str, msg: dict) -> None:
    try:
        r = redis.from_url(settings.redis_url)
        r.publish(channel, json.dumps(msg))
    except Exception:
        pass


def _make_callback(job_id: int):
    """Return an Ultralytics callback that captures per-epoch metrics."""

    def on_train_epoch_end(trainer):                                # noqa: ANN001
        try:
            epoch = int(getattr(trainer, "epoch", 0)) + 1
            total = int(getattr(trainer, "epochs", 0))
            metrics = {}
            if hasattr(trainer, "metrics"):
                metrics = {k: float(v) for k, v in (trainer.metrics or {}).items()
                           if isinstance(v, (int, float))}
            with SessionLocal() as db:
                job = db.get(TrainingJob, job_id)
                if job:
                    job.current_epoch = epoch
                    job.last_progress_at = datetime.now(timezone.utc)
                    if "metrics/mAP50(B)" in metrics:
                        m = metrics["metrics/mAP50(B)"]
                        if (job.best_map50 or 0) < m:
                            job.best_map50 = m
                    db.commit()
            _publish(f"vg:pub:training:{job_id}",
                     {"epoch": epoch, "total": total, "metrics": metrics})
        except Exception as e:
            log.warning("trainer callback error: %s", e)

    return on_train_epoch_end


def _extract_yolo_metrics(results) -> dict:
    """Pull validation metrics from an Ultralytics training results
    object. Tolerant to API drift between Ultralytics minor versions:
    tries the object-attribute path first (`results.box.map50` etc.),
    falls back to the flat `results.results_dict` keys. Each metric
    is float-or-None — every accessor is wrapped so a missing
    attribute or non-numeric value never propagates."""
    out: dict[str, float | None] = {
        "map50": None, "map50_95": None,
        "precision": None, "recall": None,
    }
    if results is None:
        return out
    # Object-attr path (detection — results.box is a Metric).
    box = getattr(results, "box", None)
    if box is not None:
        for k, attr in (("map50", "map50"), ("map50_95", "map"),
                         ("precision", "mp"), ("recall", "mr")):
            try:
                v = getattr(box, attr, None)
                if v is not None:
                    out[k] = float(v)
            except Exception:
                pass
    # Flat results_dict fallback — survives object-attr drift.
    # The "(B)" suffix marks the Box (detection) head's metrics.
    rd = getattr(results, "results_dict", None) or {}
    flat_keys = {
        "map50":     "metrics/mAP50(B)",
        "map50_95":  "metrics/mAP50-95(B)",
        "precision": "metrics/precision(B)",
        "recall":    "metrics/recall(B)",
    }
    for k, src in flat_keys.items():
        if out[k] is None:
            try:
                v = rd.get(src) if isinstance(rd, dict) else None
                if v is not None:
                    out[k] = float(v)
            except Exception:
                pass
    return out


def run_job(job_id: int) -> None:
    """Synchronous training job runner; intended to be invoked by Celery."""
    with SessionLocal() as db:
        job = db.get(TrainingJob, job_id)
        if not job:
            raise RuntimeError(f"job {job_id} not found")
        ds  = db.get(Dataset, job.dataset_id)
        if not ds:
            raise RuntimeError("dataset missing")

        cfg = job.config_json or {}
        job.status        = "running"
        job.started_at    = datetime.now(timezone.utc)
        # Stall-watchdog heartbeat — bumped here and per epoch so the
        # dispatcher can tell "training slowly" from "worker died".
        job.last_progress_at = job.started_at
        job.total_epochs  = int(cfg.get("epochs", 50))
        job.error_message = None
        db.commit()

        # Incremental fine-tune branch — load the deployed (or named)
        # model's `weights_path` as the base and lower LR/epoch
        # defaults. Picks up Phase 2's hard-negative pool via
        # `extra_negative_dataset_ids`.
        incremental = bool(cfg.get("incremental_finetune", False))
        resume_from = cfg.get("resume_from_model_id")
        resume_weights: str | None = None
        if incremental and resume_from is not None:
            parent = db.get(AIModel, int(resume_from))
            if parent is None:
                job.status, job.error_message = "failed", (
                    f"incremental fine-tune: resume_from_model_id="
                    f"{resume_from} not found")
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
                raise RuntimeError(job.error_message)
            resume_weights = parent.weights_path

        # DATASET PRE-FLIGHT — fail clean with a useful error_message
        # before any YOLO/torch import or split work runs, so operators
        # don't have to dig through ultralytics tracebacks for "image
        # not found" / "empty dataset" cases.
        ds_root = dataset_root(ds.id)
        if not ds_root.exists():
            job.status, job.error_message = "failed", (
                f"dataset directory missing: {ds_root}")
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise RuntimeError(job.error_message)

        try:
            # Strict 80/20 train/val split (test unused — every labelled
            # image contributes signal; val stays a true hold-out). A job
            # cfg may still override explicitly.
            split_dataset(db, ds.id,
                          train=float(cfg.get("split_train", 0.8)),
                          val=float(cfg.get("split_val", 0.2)),
                          test=float(cfg.get("split_test", 0.0)))
            extra_neg_ids = cfg.get("extra_negative_dataset_ids") or []
            _mix_id = cfg.get("base_mix_dataset_id")
            yaml_path = write_yolo_dataset_yaml(
                db, ds.id,
                extra_dataset_ids=[int(i) for i in extra_neg_ids] or None,
                max_neg_ratio=float(cfg.get("max_neg_ratio", 3.0)),
                mix_dataset_id=int(_mix_id) if _mix_id is not None else None,
                mix_fraction=float(getattr(settings, "base_mix_fraction", 0.18)),
            )
        except Exception as e:
            job.status, job.error_message = "failed", f"dataset prep: {e}"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise

        # Manifest pre-flight — `write_yolo_dataset_yaml` writes
        # train.txt unconditionally even when the SQL query returns
        # zero qualifying images. Without this, YOLO loads an empty
        # manifest and the loader either crashes opaquely or trains
        # on nothing. Fail clean with a specific error_message.
        train_txt = ds_root / "train.txt"
        if not train_txt.exists():
            job.status, job.error_message = "failed", (
                f"dataset prep produced no train.txt at {train_txt}")
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise RuntimeError(job.error_message)
        lines = [ln for ln in train_txt.read_text().splitlines() if ln.strip()]
        image_count = len(lines)
        if not lines:
            job.status, job.error_message = "failed", (
                "train.txt has 0 image paths — dataset has no labelled "
                "images for this detection_type")
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise RuntimeError(job.error_message)
        first = Path(lines[0])
        if not first.exists():
            job.status, job.error_message = "failed", (
                f"first training image not found on disk: {first} "
                f"(dataset/storage out of sync)")
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise RuntimeError(job.error_message)

    # DATA SANITIZATION — corrupt/duplicate/blurred images are excluded
    # (manifests rewritten; source files untouched) and extreme class
    # imbalance is surfaced before compute is spent. A sanitize failure
    # fails the job loudly — never train on an unvetted manifest.
    try:
        sanitize_report = sanitize_dataset(
            str(ds_root),
            blur_threshold=float(getattr(settings, "sanitize_blur_threshold",
                                         25.0)))
    except Exception as e:
        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status, job2.error_message = "failed", f"dataset sanitize: {e}"
                job2.completed_at = datetime.now(timezone.utc)
                db.commit()
        raise
    for warning in sanitize_report["imbalance_warnings"]:
        log.warning("training job %s: %s", job_id, warning)
    # Re-read the (possibly rewritten) manifest so epoch sizing and the
    # minimum-image gate operate on what will actually be trained on.
    image_count = len([ln for ln in (ds_root / "train.txt")
                       .read_text().splitlines() if ln.strip()])
    # Minimum-viable-dataset gate: the COMBINED post-sanitize set
    # (train + val: positives + capped negatives + replay mix) must reach
    # settings.min_training_images (default 50) — below that, validation
    # metrics are noise and the fine-tune overfits. The orchestrator
    # projects this before enqueueing; this is the authoritative check.
    from app.training.errors import InsufficientDataError
    combined = int(sanitize_report["kept"])
    min_total = int(getattr(settings, "min_training_images", 50))
    if combined < min_total:
        msg = (f"insufficient data: combined dataset has {combined} images "
               f"after sanitization (< min_training_images={min_total}; "
               f"excluded: corrupt={len(sanitize_report['excluded_corrupt'])} "
               f"dup={len(sanitize_report['excluded_duplicate'])} "
               f"blurred={len(sanitize_report['excluded_blurred'])})")
        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status, job2.error_message = "failed", msg
                job2.completed_at = datetime.now(timezone.utc)
                db.commit()
        raise InsufficientDataError(msg)

    # Reproducibility fingerprint of the staged, sanitized dataset —
    # persisted on the AIModel row and in the JSON run record.
    from app.training.training_logger import compute_dataset_hash, log_training_run
    runs_dir = Path(settings.models_dir).parent / "training_runs"
    dataset_hash: str | None = None
    try:
        dataset_hash = compute_dataset_hash(ds_root)
    except Exception as e:
        # Non-fatal for the run itself, but never silent.
        log.error("training job %s: dataset hash failed: %s", job_id, e)

    # Heavy import deferred so the rest of the app boots without torch.
    from ultralytics import YOLO

    # Fine-tune mode: start from the named model's weights with a
    # low LR + short schedule. Full-retrain mode: start from the
    # configured base_model (e.g. yolov8n.pt).
    # Base weights (Part 2 #7 — incremental fine-tuning): explicit resume >
    # currently-deployed model for this detection_type > base yolov8n. Each
    # run BUILDS on the last deployed model for faster convergence.
    parent_model_id: int | None = None
    if incremental and resume_weights:
        base_model = resume_weights
        parent_model_id = int(resume_from) if resume_from is not None else None
        lr         = cfg.get("lr0", 0.0005)         # low LR avoids forgetting
    else:
        dep_weights, dep_id = _deployed_parent_for(cfg.get("detection_type"))
        if dep_weights:
            base_model = dep_weights
            parent_model_id = dep_id
            lr         = cfg.get("lr0", 0.0005)
            log.info("training job %s: incremental base = deployed model id=%s (%s)",
                     job_id, dep_id, dep_weights)
        else:
            base_model = cfg.get("base_model", "yolov8n.pt")
            lr         = cfg.get("lr0", "auto")
    # Epochs (Part 2 #2): honour an explicit cfg override, else size to the
    # dataset — more epochs on small sets = better learning per image.
    if cfg.get("epochs"):
        epochs = int(cfg["epochs"])
    else:
        epochs = 20 if image_count < 50 else 15 if image_count < 200 else 10
    batch      = int(cfg.get("batch", 16))
    imgsz      = int(cfg.get("imgsz", 640))
    augment    = bool(cfg.get("augment", True))

    out_dir = Path(settings.models_dir) / f"job_{job_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("training job %s starting: base=%s epochs=%s batch=%s imgsz=%s",
             job_id, base_model, epochs, batch, imgsz)
    _publish(f"vg:pub:training:{job_id}",
             {"event": "start", "base_model": base_model, "epochs": epochs})

    try:
        model = YOLO(base_model)
        model.add_callback("on_train_epoch_end", _make_callback(job_id))
        train_kwargs = dict(
            data=str(yaml_path),
            epochs=epochs,
            batch=batch,
            imgsz=imgsz,
            project=str(out_dir),
            name="run",
            exist_ok=True,
            device=("0" if settings.use_gpu else "cpu"),
            augment=augment,
        )
        # Augmentation kwargs (defaults merged with any per-job override),
        # validated against the INSTALLED ultralytics' config schema so an
        # invalid key fails the job fast with a named error — the old
        # silent TypeError-retry (drop the whole aug block mid-run) is gone.
        _aug: dict[str, float] = {**DEFAULT_AUGMENTATION,
                                  **(cfg.get("augmentation") or {})}
        validate_augmentation_config(_aug)
        train_kwargs.update(_aug)
        if lr and lr != "auto":
            train_kwargs["lr0"] = float(lr)
        log.info("training job %s: augmentation args applied: %s",
                 job_id, sorted(_aug.keys()))
        # Bind the return value — it's the validation Metrics from the
        # final epoch. Previously discarded, leaving AIModel.map50 /
        # precision / recall NULL and starving the promotion gate.
        results = model.train(**train_kwargs)
        # Diagnostic — surface exactly what the results object exposes so a
        # future metrics regression (like the all-zero mAP bug) is visible in
        # the worker log instead of being silently stored as 0/None.
        log.info("YOLO results for job %s: box=%s results_dict_keys=%s",
                 job_id, getattr(results, "box", None),
                 list(getattr(results, "results_dict", {}) or {}))
        final_metrics = _extract_yolo_metrics(results)

        # Find best.pt produced by ultralytics.
        best = next(out_dir.rglob("best.pt"), None)
        if not best:
            raise RuntimeError("training finished but best.pt not found")

        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status       = "done"
                job2.completed_at = datetime.now(timezone.utc)
                job2.total_epochs = epochs      # reflect the dataset-sized value
                # Refresh best_map50 from the FINAL validation read —
                # the per-epoch callback only fires for epochs that
                # ran validation, so the final value can exceed the
                # callback's running max. Keep the larger of the two.
                final_m = final_metrics["map50"]
                if final_m is not None:
                    if (job2.best_map50 or 0) < final_m:
                        job2.best_map50 = final_m
            ds2 = db.get(Dataset, job2.dataset_id) if job2 else None
            # Auto-version: next vN for this model name. v1 if first.
            model_name = job2.model_name if job2 else f"model-{job_id}"
            existing = (db.query(AIModel)
                          .filter(AIModel.name == model_name)
                          .all())
            next_version = f"v{len(existing) + 1}"
            # All four metrics persisted on the AIModel row so the
            # promotion gate can actually compare candidates.
            ai_model = AIModel(
                name=model_name,
                version=next_version,
                base_model=base_model,
                parent_model_id=parent_model_id,
                classes_json=(ds2.classes_json if ds2 else []),
                weights_path=str(best),
                export_format="pt",
                training_job_id=job_id,
                map50=(final_metrics["map50"]
                        if final_metrics["map50"] is not None
                        else (job2.best_map50 if job2 else None)),
                map50_95=final_metrics["map50_95"],
                precision=final_metrics["precision"],
                recall=final_metrics["recall"],
                dataset_hash=dataset_hash,
                validation_metrics_json=final_metrics,
            )
            db.add(ai_model)
            db.commit()
            new_model_id = ai_model.id

            # Auto-promotion gate (config-driven — used by the cross-store
            # pipeline). When a job sets cfg["auto_promote_map50"] and the
            # trained map50 clears it, either deploy now (if
            # settings.cross_store_auto_deploy) or stage it and log that it's
            # ready for a manual promote.
            _auto = cfg.get("auto_promote_map50")
            _m50 = final_metrics.get("map50")
            if _auto is not None and _m50 is not None and _m50 >= float(_auto):
                from app.config import settings as _s
                if bool(getattr(_s, "cross_store_auto_deploy", False)):
                    # Automated deploy → must clear the model gate (class
                    # map + inference smoke test). A gate failure stages
                    # the model instead of deploying a broken one.
                    from app.ai.model_gating import validate_model_before_deploy
                    if not validate_model_before_deploy(
                            str(best), list(ai_model.classes_json or [])):
                        log.error(
                            "Cross-store auto-deploy BLOCKED by model gate "
                            "(model=%s weights=%s) — staged deployed=false; "
                            "see gate log above for the rejection reason",
                            ai_model.id, best)
                    else:
                        sibs = (db.query(AIModel)
                                  .filter(AIModel.name == ai_model.name,
                                          AIModel.deployed == True,        # noqa: E712
                                          AIModel.id != ai_model.id).all())
                        for _sib in sibs:
                            _sib.deployed = False
                        ai_model.deployed = True
                        db.commit()
                        log.info("Cross-store model promoted — now active on "
                                 "all cameras (map50=%.4f >= %.2f, model=%s)",
                                 _m50, float(_auto), ai_model.id)
                else:
                    log.info("Cross-store model map50=%.4f >= %.2f — ready to "
                             "promote (staged deployed=false; set "
                             "CROSS_STORE_AUTO_DEPLOY=true or promote manually)",
                             _m50, float(_auto))
        # Experiment record — the JSON audit trail for this run. A write
        # failure is logged at ERROR (surfaced, never silent) but does not
        # fail a training run that already completed.
        try:
            log_training_run(
                runs_dir, job_id=job_id, status="done",
                dataset_id=job.dataset_id, dataset_hash=dataset_hash,
                parent_model_id=parent_model_id, model_id=new_model_id,
                hyperparameters={k: str(v) if isinstance(v, Path) else v
                                 for k, v in train_kwargs.items()},
                augmentation_config=_aug,
                sanitize_report=sanitize_report,
                final_validation_metrics=final_metrics)
        except Exception as e:
            log.error("training job %s: run-record write failed: %s", job_id, e)
        _publish(f"vg:pub:training:{job_id}",
                 {"event": "done", "weights": str(best),
                  "metrics": final_metrics})

    except Exception as e:
        log.exception("training job %s failed: %s", job_id, e)
        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status = "failed"
                job2.error_message = str(e)
                job2.completed_at = datetime.now(timezone.utc)
                db.commit()
        try:
            log_training_run(
                runs_dir, job_id=job_id, status="failed",
                dataset_id=job.dataset_id, dataset_hash=dataset_hash,
                parent_model_id=parent_model_id,
                sanitize_report=sanitize_report, error=str(e))
        except Exception as e2:
            log.error("training job %s: failure-record write failed: %s",
                      job_id, e2)
        _publish(f"vg:pub:training:{job_id}", {"event": "failed", "error": str(e)})
        raise
