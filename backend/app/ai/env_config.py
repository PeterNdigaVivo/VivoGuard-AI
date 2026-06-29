"""Hardware auto-detection + model optimization.

Inspired by SharpAI/DeepCamera's skills/lib/env_config.py. Detects the
best available compute backend (NVIDIA CUDA → AMD ROCm → Apple MPS →
Intel OpenVINO → CPU) and, on first use, exports the YOLO model to the
backend's optimized format and caches it on disk.

IMPLEMENTATION NOTE — optimized models are loaded through Ultralytics
`YOLO(exported_path)`, NOT a raw onnxruntime/openvino session. Ultralytics
natively loads .onnx / .engine / _openvino_model / .mlpackage and keeps
the SAME `.predict() -> .boxes` API the rest of app.ai.yolov8_runner
relies on. A raw runtime session would return undecoded tensors and
force a rewrite of all NMS / box-decoding — the optimization benefit is
identical via the Ultralytics loader, with zero blast radius.

Everything here is defensively guarded: any missing framework, failed
export, or unreadable device falls back cleanly. On a CPU-only host with
neither onnxruntime nor a GPU, detect() returns the cpu backend and
load_optimized() returns a raw PyTorch YOLO — the system always runs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# backend → Ultralytics export `format=` string. rocm has no export
# path (PyTorch HIP runs the raw .pt on the AMD device), so it maps to
# None = "load the raw .pt, no export".
BACKEND_SPECS: dict[str, dict[str, Any]] = {
    "cuda":  {"export_format": "engine",   "device": "cuda"},  # TensorRT .engine
    "rocm":  {"export_format": None,        "device": "cuda"},  # PyTorch HIP, no export
    "mps":   {"export_format": "coreml",   "device": "mps"},   # CoreML .mlpackage
    "intel": {"export_format": "openvino", "device": "cpu"},   # OpenVINO IR
    "cpu":   {"export_format": "onnx",     "device": "cpu"},   # ONNX Runtime
}


@dataclass
class HardwareEnv:
    backend:       str   = "cpu"
    device:        str   = "cpu"
    export_format: str   = "onnx"
    gpu_name:      str   = ""
    gpu_memory_mb: int   = 0
    framework_ok:  bool  = False
    export_ms:     float = 0.0
    load_ms:       float = 0.0

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    @staticmethod
    def detect() -> "HardwareEnv":
        """Probe the host for the best available backend, in priority
        order: CUDA → ROCm → MPS → Intel OpenVINO → CPU. Never raises —
        any probe failure simply falls through to the next candidate."""
        env = HardwareEnv()

        # torch is the gate for the first three probes.
        torch = None
        try:
            import torch as _torch
            torch = _torch
            env.framework_ok = True
        except Exception:
            env.framework_ok = False

        if torch is not None:
            # (b) ROCm is reported THROUGH torch.cuda on AMD, and
            # torch.cuda.is_available() is also True under ROCm — so we
            # must check torch.version.hip BEFORE the NVIDIA branch to
            # disambiguate, then map ROCm to device="cuda".
            hip = getattr(getattr(torch, "version", None), "hip", None)
            try:
                cuda_ok = bool(torch.cuda.is_available())
            except Exception:
                cuda_ok = False

            if hip and cuda_ok:
                env.backend = "rocm"
                env.device  = "cuda"
                env.export_format = BACKEND_SPECS["rocm"]["export_format"] or "onnx"
                env._fill_gpu(torch)
                return env

            # (a) NVIDIA CUDA.
            if cuda_ok:
                env.backend = "cuda"
                env.device  = "cuda"
                env.export_format = BACKEND_SPECS["cuda"]["export_format"]
                env._fill_gpu(torch)
                return env

            # (c) Apple MPS.
            try:
                if torch.backends.mps.is_available():
                    env.backend = "mps"
                    env.device  = "mps"
                    env.export_format = BACKEND_SPECS["mps"]["export_format"]
                    env.gpu_name = "Apple Silicon (MPS)"
                    return env
            except Exception:
                pass

        # (d) Intel OpenVINO — independent of torch.
        try:
            import openvino  # noqa: F401
            env.backend = "intel"
            env.device  = "cpu"
            env.export_format = BACKEND_SPECS["intel"]["export_format"]
            env.gpu_name = "Intel (OpenVINO)"
            return env
        except Exception:
            pass

        # (e) Default — CPU via ONNX Runtime.
        env.backend = "cpu"
        env.device  = "cpu"
        env.export_format = BACKEND_SPECS["cpu"]["export_format"]
        return env

    def _fill_gpu(self, torch) -> None:
        """Populate gpu_name / gpu_memory_mb from the first CUDA/HIP
        device. Best-effort."""
        try:
            props = torch.cuda.get_device_properties(0)
            self.gpu_name = str(getattr(props, "name", "") or "")
            total = int(getattr(props, "total_memory", 0) or 0)
            self.gpu_memory_mb = total // (1024 * 1024)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Optimized model loading
    # ------------------------------------------------------------------
    def load_optimized(self, model_path: str, use_optimized: bool = True):
        """Return (model, fmt).

        When use_optimized is True and the backend has an export format,
        export the model to that format once (cached under
        data/models/optimized/{stem}_{backend}/) and load it through
        Ultralytics. On ANY failure — and for rocm / use_optimized=False
        — fall back to a raw PyTorch YOLO(model_path).

        `fmt` is the format actually loaded ('pytorch', 'onnx',
        'engine', 'openvino', 'coreml')."""
        from ultralytics import YOLO   # heavy import — kept lazy

        spec_format = BACKEND_SPECS.get(self.backend, {}).get("export_format")

        # No-export paths: optimization off, rocm (raw HIP), or an
        # unknown backend.
        if not use_optimized or not spec_format:
            t0 = time.perf_counter()
            model = YOLO(model_path)
            self.load_ms = (time.perf_counter() - t0) * 1000.0
            return model, "pytorch"

        try:
            from app.config import settings
            base = Path(settings.models_dir) / "optimized"
        except Exception:
            base = Path("data/models/optimized")

        stem = Path(model_path).stem
        cache_dir = base / f"{stem}_{self.backend}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        artifact = self._cached_artifact(cache_dir, stem, spec_format)

        try:
            if artifact is None or not artifact.exists():
                artifact = self._export(model_path, cache_dir, spec_format)
            t0 = time.perf_counter()
            model = YOLO(str(artifact))
            self.load_ms = (time.perf_counter() - t0) * 1000.0
            return model, spec_format
        except Exception as e:
            log.warning("env_config: optimized load failed for backend=%s "
                        "(%s) — falling back to raw PyTorch", self.backend, e)
            t0 = time.perf_counter()
            model = YOLO(model_path)
            self.load_ms = (time.perf_counter() - t0) * 1000.0
            return model, "pytorch"

    @staticmethod
    def _cached_artifact(cache_dir: Path, stem: str,
                         fmt: str) -> Optional[Path]:
        """Path the export would produce, if it already exists."""
        if fmt == "openvino":
            d = cache_dir / f"{stem}_openvino_model"
            return d if d.exists() else None
        ext = {"onnx": ".onnx", "engine": ".engine",
               "coreml": ".mlpackage"}.get(fmt)
        if not ext:
            return None
        p = cache_dir / f"{stem}{ext}"
        return p if p.exists() else None

    def _export(self, model_path: str, cache_dir: Path, fmt: str) -> Path:
        """Export model_path to `fmt`, move the artifact into cache_dir,
        return its path. Times the export into self.export_ms."""
        from ultralytics import YOLO
        t0 = time.perf_counter()
        src = YOLO(model_path)
        exported = src.export(format=fmt, device=self.device)
        self.export_ms = (time.perf_counter() - t0) * 1000.0
        # Ultralytics returns the produced path (str). Move it into the
        # cache dir so all backends share one predictable layout.
        produced = Path(exported)
        target = cache_dir / produced.name
        try:
            if produced.resolve() != target.resolve():
                import shutil
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                shutil.move(str(produced), str(target))
            return target
        except Exception:
            # Move failed — use the produced path in place rather than
            # re-export every run.
            return produced

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)
