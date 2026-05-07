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

log = logging.getLogger(__name__)


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
        job.total_epochs  = int(cfg.get("epochs", 50))
        job.error_message = None
        db.commit()

        try:
            split_dataset(db, ds.id,
                          train=float(cfg.get("split_train", 0.7)),
                          val=float(cfg.get("split_val", 0.2)),
                          test=float(cfg.get("split_test", 0.1)))
            yaml_path = write_yolo_dataset_yaml(db, ds.id)
        except Exception as e:
            job.status, job.error_message = "failed", f"dataset prep: {e}"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise

    # Heavy import deferred so the rest of the app boots without torch.
    from ultralytics import YOLO

    base_model = cfg.get("base_model", "yolov8n.pt")
    epochs     = int(cfg.get("epochs", 50))
    batch      = int(cfg.get("batch", 16))
    imgsz      = int(cfg.get("imgsz", 640))
    lr         = cfg.get("lr0", "auto")
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
        if lr and lr != "auto":
            train_kwargs["lr0"] = float(lr)
        model.train(**train_kwargs)

        # Find best.pt produced by ultralytics.
        best = next(out_dir.rglob("best.pt"), None)
        if not best:
            raise RuntimeError("training finished but best.pt not found")

        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status       = "done"
                job2.completed_at = datetime.now(timezone.utc)
            ds2 = db.get(Dataset, job2.dataset_id) if job2 else None
            ai_model = AIModel(
                name=job2.model_name if job2 else f"model-{job_id}",
                version="v1",
                base_model=base_model,
                classes_json=(ds2.classes_json if ds2 else []),
                weights_path=str(best),
                export_format="pt",
                training_job_id=job_id,
                map50=(job2.best_map50 if job2 else None),
            )
            db.add(ai_model)
            db.commit()
        _publish(f"vg:pub:training:{job_id}",
                 {"event": "done", "weights": str(best)})

    except Exception as e:
        log.exception("training job %s failed: %s", job_id, e)
        with SessionLocal() as db:
            job2 = db.get(TrainingJob, job_id)
            if job2:
                job2.status = "failed"
                job2.error_message = str(e)
                job2.completed_at = datetime.now(timezone.utc)
                db.commit()
        _publish(f"vg:pub:training:{job_id}", {"event": "failed", "error": str(e)})
        raise
