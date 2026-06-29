"""Person-appearance encoder for cross-camera Re-ID (Sprint 2.2).

  HSV COLOUR-HISTOGRAM encoder (Option A — zero ML, instant).

The original ResNet18 embedder was unusable on the CPU-only Hetzner box:
~32,000 ms per crop. A deep feature extractor is simply the wrong tool on
this hardware. We replace it with a classic appearance descriptor — an
HSV colour histogram — which captures "what is this person wearing"
(shirt/trousers colour distribution) well enough to re-identify the same
shopper across cameras minutes apart, at ~sub-millisecond cost using only
OpenCV + NumPy (both already in the base worker image).

    crop (BGR HxWx3 uint8)  ->  128-dim L2-normalized float32 vector
                                (64 H bins + 32 S bins + 32 V bins)

Trade-off vs deep embeddings: a colour histogram is less discriminative
(two people in the same uniform look alike), so the matcher uses a
slightly lower cosine threshold (0.70) to compensate. For Vivo's use case
— de-duplicating one shopper across a handful of store cameras within a
4h window — colour appearance is a strong, cheap signal.

Everything here is BEST-EFFORT: OpenCV may be absent, a crop may be
degenerate. Every public entry point returns None on any failure and
NEVER raises into the inference hot path. The public interface (encode,
instance(), get_reid_encoder) is unchanged, so nothing downstream changes.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# Descriptor dimensionality: 64 (H) + 32 (S) + 32 (V) = 128.
_H_BINS, _S_BINS, _V_BINS = 64, 32, 32
EMBED_DIM = _H_BINS + _S_BINS + _V_BINS

# OpenCV HSV ranges: H is 0-179 (8-bit), S and V are 0-255.
_H_RANGE = [0, 180]
_SV_RANGE = [0, 256]


class ReIDEncoder:
    """Singleton HSV-histogram appearance encoder.

    Kept as a class with the same shape as the previous deep-model
    version so the inference loop and tracker need no changes. There's no
    model to load now, so construction is free; the singleton pattern is
    retained purely for interface stability.
    """

    _instance: "ReIDEncoder | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._cv2 = None             # lazily imported OpenCV handle
        self._load_failed = False    # latch so a missing cv2 doesn't retry every frame
        self._lock = threading.Lock()

    # -- construction -------------------------------------------------
    @classmethod
    def instance(cls) -> "ReIDEncoder":
        """Process-wide singleton."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_cv2(self) -> bool:
        """Lazily import OpenCV. Returns True when usable."""
        if self._cv2 is not None:
            return True
        if self._load_failed:
            return False
        with self._lock:
            if self._cv2 is not None:
                return True
            if self._load_failed:
                return False
            try:
                import cv2
                self._cv2 = cv2
                log.info("ReIDEncoder: HSV colour-histogram encoder ready (%d-dim)", EMBED_DIM)
                return True
            except Exception as e:                      # noqa: BLE001
                self._load_failed = True
                log.warning("ReIDEncoder unavailable — OpenCV missing (Re-ID disabled): %s", e)
                return False

    # -- inference ----------------------------------------------------
    def encode(self, crop_bgr: np.ndarray) -> np.ndarray | None:
        """Encode a single person crop to a unit-length 128-d histogram.

        `crop_bgr` is an HxWx3 uint8 BGR array (a slice of the inference
        frame). Returns float32[128] L2-normalized, or None on any
        failure (bad crop, OpenCV unavailable, runtime error).
        """
        if crop_bgr is None or not self._ensure_cv2():
            return None
        try:
            if crop_bgr.ndim != 3 or crop_bgr.shape[0] < 4 or crop_bgr.shape[1] < 4:
                return None
            cv2 = self._cv2
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            # Per-channel 1-D histograms, then concatenate. Computing
            # three small 1-D histograms (128 bins total) is far cheaper
            # and less sparse than one 3-D histogram.
            h = cv2.calcHist([hsv], [0], None, [_H_BINS], _H_RANGE)
            s = cv2.calcHist([hsv], [1], None, [_S_BINS], _SV_RANGE)
            v = cv2.calcHist([hsv], [2], None, [_V_BINS], _SV_RANGE)
            vec = np.concatenate([h, s, v]).astype(np.float32).ravel()
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
