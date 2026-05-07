"""YOLOv8 inference wrapper.

Loads a model once per process, runs inference on numpy frames, and
returns a normalised list of detections regardless of model class set.

Detections are dicts with: cls (str), conf (float), bbox_norm
([x1,y1,x2,y2] in [0..1]). Pixel-space bbox is also included as `bbox_px`.
"""
from __future__ import annotations
import logging
import os
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Model objects keyed by weights path. Lazy-loaded the first time.
_models: dict[str, object] = {}
_lock = threading.Lock()


def _device() -> str:
    """`cuda:0` when GPU is available and enabled, else `cpu`."""
    from app.config import settings
    if not settings.use_gpu:
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:
        pass
    return "cpu"


def load_model(weights: str):
    """Load (and cache) a YOLOv8 model from a `.pt` file path."""
    with _lock:
        if weights not in _models:
            from ultralytics import YOLO          # heavy import — kept lazy
            log.info("loading YOLO model: %s on %s", weights, _device())
            model = YOLO(weights)
            _models[weights] = model
        return _models[weights]


def resolve_weights(model_name: str | None = None) -> str:
    """Resolve a friendly name (e.g. 'yolov8n.pt') to an absolute path."""
    from app.config import settings
    name = model_name or settings.default_model
    if os.path.isabs(name):
        return name
    candidate = Path(settings.models_dir) / name
    if candidate.exists():
        return str(candidate)
    # Ultralytics will download yolov8n.pt etc. into CWD on first use; that
    # works inside the container.
    return name


def infer(frame: np.ndarray, *, weights: str | None = None,
          conf: float = 0.25, imgsz: int = 640) -> list[dict]:
    """Run inference on a single BGR frame.
    Returns a list of detection dicts."""
    model = load_model(resolve_weights(weights))
    results = model.predict(frame, conf=conf, imgsz=imgsz, device=_device(),
                            verbose=False)
    out: list[dict] = []
    if not results:
        return out
    r = results[0]
    if r.boxes is None or len(r.boxes) == 0:
        return out
    h, w = frame.shape[:2]
    names: dict[int, str] = r.names if hasattr(r, "names") else getattr(model, "names", {})
    xyxy = r.boxes.xyxy.cpu().numpy()
    confs = r.boxes.conf.cpu().numpy()
    clss  = r.boxes.cls.cpu().numpy().astype(int)
    for (x1, y1, x2, y2), c, k in zip(xyxy, confs, clss):
        cls_name = names.get(int(k), str(int(k)))
        out.append({
            "cls":  cls_name,
            "conf": float(c),
            "bbox_px":   [float(x1), float(y1), float(x2), float(y2)],
            "bbox_norm": [float(x1)/w, float(y1)/h, float(x2)/w, float(y2)/h],
        })
    return out
