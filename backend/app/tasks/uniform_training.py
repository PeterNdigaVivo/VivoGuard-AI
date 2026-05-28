"""Uniform-compliance classification training pipeline.

Trains a YOLOv8n-cls classifier on the uniform_ok / uniform_violation
/ no_lanyard / civilian frames collected through /training/uniform.
Mirrors the shutter pipeline but with four classes and augmentation
tuned for staff (horizontal flip allowed, wider brightness range,
slight rotation) since staff face either direction and the counter
lighting varies through the day.

Progress + result published to vg:shutter-style status key the API
polls: vg:uniform_train:status:{store_id}.
"""
from __future__ import annotations
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

MIN_PER_CLASS = 30
LABELS = ("uniform_ok", "uniform_violation", "no_lanyard", "civilian")


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _set_status(store_id: int, payload: dict) -> None:
    try:
        r = _redis()
        r.set(f"vg:uniform_train:status:{store_id}", json.dumps(payload), ex=24 * 3600)
        r.publish(f"vg:pub:uniform_train:{store_id}", json.dumps(payload))
    except Exception:
        pass


@celery_app.task(name="training.train_uniform_model", bind=True, ignore_result=True)
def train_uniform_model(self, store_id: int, camera_id: int | None = None) -> None:
    from app.database import SessionLocal
    from app.models import AIModel, Camera, TrainingSample

    _set_status(store_id, {"state": "preparing", "message": "Building dataset…"})

    with SessionLocal() as db:
        q = (db.query(TrainingSample)
               .filter(TrainingSample.detector_type == "uniform",
                       TrainingSample.store_id == store_id))
        if camera_id is not None:
            q = q.filter(TrainingSample.camera_id == camera_id)
        samples = q.all()

    by_label: dict[str, list[str]] = {l: [] for l in LABELS}
    for s in samples:
        if s.label in by_label and Path(s.frame_path).exists():
            by_label[s.label].append(s.frame_path)

    short = {l: len(by_label[l]) for l in LABELS if len(by_label[l]) < MIN_PER_CLASS}
    if short:
        msg = ("Need at least %d frames per class. Short: %s"
               % (MIN_PER_CLASS, ", ".join(f"{k}={v}" for k, v in short.items())))
        _set_status(store_id, {"state": "failed", "message": msg})
        log.warning("uniform training store %s aborted: %s", store_id, msg)
        return

    root = Path(settings.models_dir).parent / "uniform_datasets" / f"store_{store_id}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    for split in ("train", "val"):
        for label in LABELS:
            (root / split / label).mkdir(parents=True, exist_ok=True)

    import random
    for label, paths in by_label.items():
        random.shuffle(paths)
        cut = max(1, int(len(paths) * 0.8))
        for i, src in enumerate(paths):
            split = "train" if i < cut else "val"
            try:
                shutil.copy(src, root / split / label / f"{i}_{Path(src).name}")
            except Exception:
                continue

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(settings.models_dir) / f"uniform_store_{store_id}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    _set_status(store_id, {"state": "training", "message": "Training YOLOv8n-cls…",
                           "epoch": 0, "total_epochs": 50})
    try:
        from ultralytics import YOLO
        model = YOLO("yolov8n-cls.pt")

        def _on_epoch(trainer):
            try:
                epoch = int(getattr(trainer, "epoch", 0)) + 1
                _set_status(store_id, {"state": "training", "message": f"Epoch {epoch}/50",
                                       "epoch": epoch, "total_epochs": 50})
            except Exception:
                pass
        model.add_callback("on_train_epoch_end", _on_epoch)

        model.train(
            data=str(root), epochs=50, imgsz=224, batch=16,
            project=str(out_dir), name="run", exist_ok=True,
            device=("0" if settings.use_gpu else "cpu"),
            # Staff face either direction → horizontal flip is fine.
            # Wider brightness + slight rotation for lighting/angle variety.
            fliplr=0.5, flipud=0.0, degrees=10, hsv_v=0.3, scale=0.1,
        )

        best = next(out_dir.rglob("best.pt"), None)
        if not best:
            raise RuntimeError("training finished but best.pt not found")

        report = _validate(best, root)

        with SessionLocal() as db:
            store_cams = db.query(Camera).filter(Camera.store_id == store_id).all()
            ai_model = AIModel(
                name=f"Uniform — store {store_id}", version="v1",
                base_model="yolov8n-cls.pt", classes_json=list(LABELS),
                weights_path=str(best), export_format="pt",
                map50=report.get("accuracy"),
                precision=report.get("precision_macro"),
                recall=report.get("recall_macro"),
            )
            db.add(ai_model); db.commit(); db.refresh(ai_model)
            model_id = ai_model.id

        result = {
            "state": "done", "message": "Training complete",
            "model_id": model_id, "weights": str(best), "report": report,
            "candidate_cameras": [c.id for c in store_cams],
        }
        _set_status(store_id, result)
        _notify(store_id, f"uniform_store_{store_id}_{ts}.pt", report)
        log.info("uniform training store %s done: model=%s acc=%s",
                 store_id, model_id, report.get("accuracy"))
    except Exception as e:
        log.exception("uniform training store %s failed: %s", store_id, e)
        _set_status(store_id, {"state": "failed", "message": str(e)})
        raise


def _validate(weights: Path, dataset_root: Path) -> dict:
    try:
        from ultralytics import YOLO
        import numpy as np
        m = YOLO(str(weights))
        metrics = m.val(data=str(dataset_root), split="val", verbose=False)
        acc = float(getattr(metrics, "top1", 0.0) or 0.0)
        report: dict = {"accuracy": round(acc, 4)}
        cm = getattr(metrics, "confusion_matrix", None)
        matrix = getattr(cm, "matrix", None) if cm is not None else None
        if matrix is not None:
            mat = np.array(matrix, dtype=float)
            n = len(LABELS)
            mat = mat[:n, :n]
            per_class, precisions, recalls = {}, [], []
            for i, label in enumerate(LABELS):
                tp = mat[i, i]; col = mat[:, i].sum(); row = mat[i, :].sum()
                prec = (tp / col) if col else 0.0
                rec = (tp / row) if row else 0.0
                per_class[label] = {"precision": round(prec, 3), "recall": round(rec, 3)}
                precisions.append(prec); recalls.append(rec)
            report["per_class"] = per_class
            report["precision_macro"] = round(sum(precisions) / len(precisions), 3)
            report["recall_macro"] = round(sum(recalls) / len(recalls), 3)
        report["recommendation"] = (
            "Deploy to production ✅" if report.get("accuracy", 0) >= 0.85
            else "Collect more frames and retrain before deploying ⚠️")
        return report
    except Exception as e:
        log.warning("uniform validation failed: %s", e)
        return {"accuracy": None, "recommendation": "Validation unavailable"}


def _notify(store_id: int, model_name: str, report: dict) -> None:
    try:
        acc = report.get("accuracy")
        lines = [f"Uniform model training complete for store {store_id}.",
                 f"Model: {model_name}",
                 f"Accuracy: {round(acc * 100, 1)}%" if acc is not None else "Accuracy: n/a"]
        for label, pc in (report.get("per_class") or {}).items():
            lines.append(f"  {label}: {int(pc['precision']*100)}% prec, {int(pc['recall']*100)}% rec")
        lines.append(report.get("recommendation", ""))
        from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
        to = _format_whatsapp_recipient(getattr(settings, "dashboard_alert_to", ""))
        if to:
            _send_whatsapp([to], "\n".join(lines))
    except Exception:
        pass
