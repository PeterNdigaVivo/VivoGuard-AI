"""Run YOLO val on a model + dataset, return mAP, P, R, F1, confusion."""
from __future__ import annotations
import logging
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models import AIModel
from app.training.dataset import write_yolo_dataset_yaml

log = logging.getLogger(__name__)


def evaluate(model_id: int, dataset_id: int) -> dict:
    from ultralytics import YOLO

    with SessionLocal() as db:
        model_row = db.get(AIModel, model_id)
        if not model_row:
            raise ValueError("model not found")
        weights = model_row.weights_path
        yaml_path = write_yolo_dataset_yaml(db, dataset_id)

    yolo = YOLO(weights)
    res = yolo.val(data=str(yaml_path),
                   project=str(Path(settings.models_dir) / f"eval_{model_id}_{dataset_id}"),
                   exist_ok=True,
                   device=("0" if settings.use_gpu else "cpu"))

    out: dict = {}
    if res and getattr(res, "box", None):
        b = res.box
        out["mAP50"]    = float(getattr(b, "map50", 0.0) or 0.0)
        out["mAP50_95"] = float(getattr(b, "map", 0.0) or 0.0)
        out["mp"]       = float(getattr(b, "mp", 0.0) or 0.0)        # mean precision
        out["mr"]       = float(getattr(b, "mr", 0.0) or 0.0)        # mean recall

        # Per-class arrays.
        try:
            ap = b.maps                       # per-class mAP50-95
            out["per_class_map"] = [float(x) for x in (ap or [])]
        except Exception:
            pass

    # Persist top-line metrics back onto the model row.
    with SessionLocal() as db:
        m = db.get(AIModel, model_id)
        if m:
            m.map50    = out.get("mAP50")
            m.map50_95 = out.get("mAP50_95")
            m.precision = out.get("mp")
            m.recall    = out.get("mr")
            db.commit()
    return out
