"""Lightweight person-appearance encoder for cross-camera Re-ID (Sprint 2.2).

A full Re-ID stack (yolov7_reid / OSNet) is too heavy for the CPU-only
Hetzner box. Instead we reuse the torchvision ResNet18 that already ships
in the CPU worker image (`requirements.worker.cpu.txt`: torchvision>=0.18)
as a generic appearance embedder:

    crop (BGR HxWx3 uint8)  ->  512-dim L2-normalized float32 vector

ResNet18's penultimate layer (after global average pool, before the 1000-
class FC head) is a 512-dim descriptor. It isn't Re-ID-fine-tuned, but for
"is this the SAME person who was at the entrance 30s ago, in the same
store, same clothes, same lighting" the ImageNet features cluster well
enough at a 0.75 cosine threshold — and it costs ~15-30ms/crop on CPU,
which we only pay every Nth frame per track.

Everything here is BEST-EFFORT: torch may be absent (api-only image),
weights may fail to download, a crop may be degenerate. Every public
entry point returns None on any failure and NEVER raises into the
inference hot path.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# Embedding dimensionality of ResNet18's global-pool output.
EMBED_DIM = 512

# Person-Re-ID standard crop size (h, w). Taller-than-wide matches the
# human silhouette better than a square ImageNet crop.
_INPUT_H, _INPUT_W = 256, 128

# ImageNet normalization — the weights were trained with these.
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


class ReIDEncoder:
    """Singleton wrapper around a CPU ResNet18 feature extractor.

    The model loads lazily on first `encode()` so importing this module
    is cheap and safe even where torch isn't installed. All torch usage
    is contained behind try/except — a missing dependency degrades Re-ID
    to a no-op rather than crashing the worker.
    """

    _instance: "ReIDEncoder | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None          # torch.nn.Module once loaded
        self._torch = None          # the torch module handle
        self._transform = None      # (mean, std) tensors, built with the model
        self._load_failed = False   # latch so we don't retry a broken import every frame
        self._lock = threading.Lock()

    # -- construction -------------------------------------------------
    @classmethod
    def instance(cls) -> "ReIDEncoder":
        """Process-wide singleton — weights load once per worker."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_model(self) -> bool:
        """Lazily build the model. Returns True when usable."""
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        with self._lock:
            if self._model is not None:
                return True
            if self._load_failed:
                return False
            try:
                import torch
                from torchvision.models import ResNet18_Weights, resnet18

                model = resnet18(weights=ResNet18_Weights.DEFAULT)
                # Drop the 1000-class classifier; keep the 512-dim
                # global-pool descriptor as the network's output.
                model.fc = torch.nn.Identity()
                model.eval()
                # CPU-only by design; never grab a GPU even if present so
                # we don't contend with training jobs.
                model.to("cpu")

                self._torch = torch
                self._model = model
                self._mean = torch.tensor(_MEAN).view(1, 3, 1, 1)
                self._std = torch.tensor(_STD).view(1, 3, 1, 1)
                log.info("ReIDEncoder: ResNet18 feature extractor ready (CPU, %d-dim)", EMBED_DIM)
                return True
            except Exception as e:                      # noqa: BLE001
                self._load_failed = True
                log.warning("ReIDEncoder unavailable (Re-ID disabled): %s", e)
                return False

    # -- inference ----------------------------------------------------
    def encode(self, crop_bgr: np.ndarray) -> np.ndarray | None:
        """Encode a single person crop to a unit-length 512-d embedding.

        `crop_bgr` is an HxWx3 uint8 BGR array (a slice of the inference
        frame). Returns float32[512] L2-normalized, or None on any
        failure (bad crop, model unavailable, runtime error).
        """
        if crop_bgr is None or not self._ensure_model():
            return None
        try:
            if crop_bgr.ndim != 3 or crop_bgr.shape[0] < 4 or crop_bgr.shape[1] < 4:
                return None
            torch = self._torch
            # BGR -> RGB, HWC uint8 -> CHW float[0,1].
            rgb = np.ascontiguousarray(crop_bgr[:, :, ::-1])
            t = torch.from_numpy(rgb).permute(2, 0, 1).float().div_(255.0).unsqueeze(0)
            # Resize to the Re-ID input size, then ImageNet-normalize.
            t = torch.nn.functional.interpolate(
                t, size=(_INPUT_H, _INPUT_W), mode="bilinear", align_corners=False)
            t = (t - self._mean) / self._std
            with torch.no_grad():
                feat = self._model(t)          # [1, 512]
            vec = feat.squeeze(0).cpu().numpy().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm < 1e-6:
                return None
            return vec / norm
        except Exception as e:                          # noqa: BLE001
            log.debug("ReIDEncoder.encode failed: %s", e)
            return None


def get_reid_encoder() -> ReIDEncoder:
    """Convenience accessor for the process-wide encoder singleton."""
    return ReIDEncoder.instance()
