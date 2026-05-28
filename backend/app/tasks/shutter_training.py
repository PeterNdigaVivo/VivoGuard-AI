"""Shutter / door-status classification training pipeline.

Trains a YOLOv8n-cls classifier on the OPEN / CLOSED / PARTIAL frames
collected through /training/shutter. Distinct from the bbox object-
detection trainer (app.training.trainer) because classification wants
a folder-per-class layout and a different YOLO task.

Flow:
  1. Build an Ultralytics classification dataset directory:
       <root>/train/<label>/*.jpg
       <root>/val/<label>/*.jpg          (80/20 split per class)
  2. Train yolov8n-cls.
  3. Read the validation confusion matrix → per-class precision/recall
     + overall top-1 accuracy.
  4. Persist an AIModel row pointing at best.pt.
  5. Publish progress on vg:pub:shutter_train:{store_id} and email a
     completion summary.

Progress + result are also written back to a lightweight Redis status
key the API polls: vg:shutter_train:status:{store_id}.
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

MIN_PER_CLASS = 50
LABELS = ("open", "closed", "partial")


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _set_status(store_id: int, payload: dict) -> None:
    try:
        r = _redis()
        r.set(f"vg:shutter_train:status:{store_id}",
              json.dumps(payload), ex=24 * 3600)
        r.publish(f"vg:pub:shutter_train:{store_id}", json.dumps(payload))
    except Exception:
        pass


@celery_app.task(name="training.train_shutter_model", bind=True, ignore_result=True)
def train_shutter_model(self, store_id: int, camera_id: int | None = None) -> None:
    """Train a shutter classifier for one store (optionally scoped to
    a single camera's samples). store_id is also the key the UI polls."""
    from app.database import SessionLocal
    from app.models import AIModel, Camera, TrainingSample

    _set_status(store_id, {"state": "preparing", "message": "Building dataset…"})

    # ---- Gather samples ------------------------------------------------
    with SessionLocal() as db:
        q = (db.query(TrainingSample)
               .filter(TrainingSample.detector_type == "shutter",
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
        log.warning("shutter training store %s aborted: %s", store_id, msg)
        return

    # ---- Build the classification dataset dir (80/20 split) ------------
    root = Path(settings.models_dir).parent / "shutter_datasets" / f"store_{store_id}"
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
            dst = root / split / label / f"{i}_{Path(src).name}"
            try:
                shutil.copy(src, dst)
            except Exception:
                continue

    # ---- Train ---------------------------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(settings.models_dir) / f"shutter_store_{store_id}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    _set_status(store_id, {"state": "training",
                           "message": "Training YOLOv8n-cls…", "epoch": 0,
                           "total_epochs": 50})
    try:
        from ultralytics import YOLO
        model = YOLO("yolov8n-cls.pt")

        def _on_epoch(trainer):
            try:
                epoch = int(getattr(trainer, "epoch", 0)) + 1
                _set_status(store_id, {"state": "training",
                                       "message": f"Epoch {epoch}/50",
                                       "epoch": epoch, "total_epochs": 50})
            except Exception:
                pass
        model.add_callback("on_train_epoch_end", _on_epoch)

        model.train(
            data=str(root),
            epochs=50,
            imgsz=224,
            batch=16,
            project=str(out_dir),
            name="run",
            exist_ok=True,
            device=("0" if settings.use_gpu else "cpu"),
            # Shutter orientation is meaningful — never horizontal-flip.
            # Mild brightness / blur / noise simulate dirty lenses + dusk.
            fliplr=0.0, flipud=0.0,
            hsv_v=0.2, degrees=0, translate=0.05, scale=0.1,
        )

        best = next(out_dir.rglob("best.pt"), None)
        if not best:
            raise RuntimeError("training finished but best.pt not found")

        # ---- Validation report ----------------------------------------
        report = _validate(best, root)

        with SessionLocal() as db:
            store_cams = (db.query(Camera)
                            .filter(Camera.store_id == store_id).all())
            ai_model = AIModel(
                name=f"Shutter — store {store_id}",
                version="v1",
                base_model="yolov8n-cls.pt",
                classes_json=list(LABELS),
                weights_path=str(best),
                export_format="pt",
                map50=report.get("accuracy"),
                precision=report.get("precision_macro"),
                recall=report.get("recall_macro"),
            )
            db.add(ai_model)
            db.commit()
            db.refresh(ai_model)
            model_id = ai_model.id

        result = {
            "state": "done",
            "message": "Training complete",
            "model_id": model_id,
            "weights": str(best),
            "report": report,
            "candidate_cameras": [c.id for c in store_cams],
        }
        _set_status(store_id, result)
        _email_summary(store_id, ai_model_name=f"shutter_store_{store_id}_{ts}.pt",
                       report=report)
        log.info("shutter training store %s done: model=%s acc=%.3f",
                 store_id, model_id, report.get("accuracy", 0.0))

    except Exception as e:
        log.exception("shutter training store %s failed: %s", store_id, e)
        _set_status(store_id, {"state": "failed", "message": str(e)})
        raise


def _validate(weights: Path, dataset_root: Path) -> dict:
    """Run validation and derive top-1 accuracy + per-class precision/
    recall from the confusion matrix. Best-effort — returns whatever
    metrics ultralytics exposes for this version."""
    try:
        from ultralytics import YOLO
        m = YOLO(str(weights))
        metrics = m.val(data=str(dataset_root), split="val", verbose=False)
        acc = float(getattr(getattr(metrics, "top1", None), "__float__", lambda: 0.0)()) \
            if hasattr(metrics, "top1") else None
        # top1 is usually a plain float attribute.
        if acc is None or acc == 0.0:
            acc = float(getattr(metrics, "top1", 0.0) or 0.0)
        report: dict = {"accuracy": round(acc, 4)}

        cm = getattr(metrics, "confusion_matrix", None)
        matrix = getattr(cm, "matrix", None) if cm is not None else None
        if matrix is not None:
            import numpy as np
            mat = np.array(matrix, dtype=float)
            # Ultralytics confusion matrix is (n+1)x(n+1) incl. background;
            # slice to the class block.
            n = len(LABELS)
            mat = mat[:n, :n]
            per_class = {}
            precisions, recalls = [], []
            for i, label in enumerate(LABELS):
                tp = mat[i, i]
                col = mat[:, i].sum()    # predicted-as-i
                row = mat[i, :].sum()    # actually-i
                prec = (tp / col) if col else 0.0
                rec  = (tp / row) if row else 0.0
                per_class[label] = {"precision": round(prec, 3),
                                    "recall": round(rec, 3)}
                precisions.append(prec); recalls.append(rec)
            report["per_class"] = per_class
            report["precision_macro"] = round(sum(precisions) / len(precisions), 3)
            report["recall_macro"] = round(sum(recalls) / len(recalls), 3)
        report["recommendation"] = (
            "Deploy to production ✅" if report.get("accuracy", 0) >= 0.85
            else "Collect more frames and retrain before deploying ⚠️"
        )
        return report
    except Exception as e:
        log.warning("shutter validation failed: %s", e)
        return {"accuracy": None, "recommendation": "Validation unavailable"}


def _email_summary(store_id: int, ai_model_name: str, report: dict) -> None:
    """Best-effort completion email reusing the briefing WhatsApp/email
    helpers. Silent no-op when notifications aren't configured."""
    try:
        acc = report.get("accuracy")
        lines = [f"Shutter model training complete for store {store_id}.",
                 f"Model: {ai_model_name}",
                 f"Accuracy: {round(acc * 100, 1)}%" if acc is not None else "Accuracy: n/a"]
        for label, pc in (report.get("per_class") or {}).items():
            lines.append(f"  {label}: {int(pc['precision']*100)}% precision, "
                         f"{int(pc['recall']*100)}% recall")
        lines.append(report.get("recommendation", ""))
        body = "\n".join(lines)
        from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
        to = _format_whatsapp_recipient(getattr(settings, "dashboard_alert_to", ""))
        if to:
            _send_whatsapp([to], body)
    except Exception:
        pass
