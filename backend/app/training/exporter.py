"""Export trained models to TorchScript / ONNX / TensorRT."""
from __future__ import annotations
import logging

from app.database import SessionLocal
from app.models import AIModel

log = logging.getLogger(__name__)

VALID_FORMATS = {"torchscript", "onnx", "engine"}    # 'engine' = TensorRT


def export(model_id: int, fmt: str) -> str:
    """Export and return the new artifact path."""
    if fmt not in VALID_FORMATS:
        raise ValueError(f"unsupported format: {fmt}")
    from ultralytics import YOLO

    with SessionLocal() as db:
        m = db.get(AIModel, model_id)
        if not m:
            raise ValueError("model not found")
        weights = m.weights_path

    yolo = YOLO(weights)
    out_path = yolo.export(format=fmt)         # returns path to artefact
    out_path = str(out_path)

    with SessionLocal() as db:
        m = db.get(AIModel, model_id)
        if m:
            m.export_format = fmt
            # Keep .pt as the primary; record exported alongside.
            log.info("export %s -> %s", m.weights_path, out_path)
            db.commit()
    return out_path
