"""Annotation helpers — auto-suggest labels using a base model."""
from __future__ import annotations
import logging
from pathlib import Path

import numpy as np
from PIL import Image

from app.ai.yolov8_runner import infer

log = logging.getLogger(__name__)


def auto_suggest(image_path: str, *, weights: str | None = None,
                 conf: float = 0.4) -> list[dict]:
    """Run a base model over an image and return [{class_label, bbox_norm}].
    bbox is in YOLO (cx, cy, w, h) normalised form so it slots into the
    Annotation.bbox_json field directly.
    """
    p = Path(image_path)
    if not p.exists():
        return []
    img = Image.open(p).convert("RGB")
    arr = np.array(img)[:, :, ::-1]      # RGB → BGR

    raw = infer(arr, weights=weights, conf=conf)
    out: list[dict] = []
    for d in raw:
        x1, y1, x2, y2 = d["bbox_norm"]
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        w  = max(0.0, x2 - x1)
        h  = max(0.0, y2 - y1)
        out.append({
            "class_label": d["cls"],
            "bbox_json":   [cx, cy, w, h],
            "confidence":  d["conf"],
            "auto_suggested": True,
        })
    return out
