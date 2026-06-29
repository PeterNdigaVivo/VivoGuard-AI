"""YOLOv8 inference wrapper.

Loads a model once per process (optimized for the detected hardware
backend via app.ai.env_config), runs inference on numpy frames, and
returns a normalised list of detections regardless of model class set.

Detections are dicts with: cls (str), conf (float), bbox_norm
([x1,y1,x2,y2] in [0..1]). Pixel-space bbox is also included as `bbox_px`.
"""
from __future__ import annotations
import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

from app.ai.env_config import HardwareEnv

log = logging.getLogger(__name__)

# Model objects keyed by weights path. Lazy-loaded the first time.
_models: dict[str, object] = {}
_lock = threading.Lock()

# Hardware backend is detected ONCE per process at first model load.
# CUDA → ROCm → MPS → Intel OpenVINO → CPU (see env_config.detect()).
_env: HardwareEnv | None = None


def _hardware() -> HardwareEnv:
    global _env
    if _env is None:
        _env = HardwareEnv.detect()
        # One-line backend banner to stderr so it shows up in
        # `docker compose logs` regardless of log config.
        print(
            f"[VivoGuard AI] backend={_env.backend} device={_env.device} "
            f"format={_env.export_format} gpu={_env.gpu_name or 'none'} "
            f"gpu_mem_mb={_env.gpu_memory_mb} framework_ok={_env.framework_ok}",
            file=sys.stderr, flush=True,
        )
    return _env


def load_model(weights: str):
    """Load (and cache) a YOLO model for `weights`, optimized for the
    detected backend when settings.use_optimized is True."""
    with _lock:
        if weights not in _models:
            from app.config import settings
            env = _hardware()
            use_opt = bool(getattr(settings, "use_optimized", True))
            model, fmt = env.load_optimized(weights, use_optimized=use_opt)
            log.info("loaded YOLO model %s as %s (backend=%s device=%s "
                     "export_ms=%.0f load_ms=%.0f)",
                     weights, fmt, env.backend, env.device,
                     env.export_ms, env.load_ms)
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
    device = _hardware().device
    results = model.predict(frame, conf=conf, imgsz=imgsz, device=device,
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
