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
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def configure_cpu_thread_budget(backend: str) -> int:
    """Bound native compute pools in each CPU inference process.

    Celery already provides process-level parallelism. Letting OpenCV and
    PyTorch independently use every host core inside every child creates a
    multiplicative thread storm. Environment variables cover libraries that
    initialise before this function; explicit calls cover already-imported
    runtimes. GPU backends retain their framework defaults.
    """
    if backend not in {"cpu", "intel"}:
        return 0

    try:
        threads = max(1, int(os.getenv("INFERENCE_LIBRARY_THREADS", "1")))
    except (TypeError, ValueError):
        threads = 1

    try:
        import cv2
        cv2.setNumThreads(threads)
    except Exception as exc:
        log.debug("unable to set OpenCV thread budget: %s", exc)

    try:
        import torch
        torch.set_num_threads(threads)
        try:
            torch.set_num_interop_threads(threads)
        except RuntimeError:
            # PyTorch permits this setting only before inter-op work starts.
            pass
    except Exception as exc:
        log.debug("unable to set PyTorch thread budget: %s", exc)

    _configure_onnxruntime_sessions(threads)

    log.info("CPU inference native thread budget=%d per worker process", threads)
    return threads


def _configure_onnxruntime_sessions(threads: int) -> bool:
    """Supply bounded SessionOptions to Ultralytics' ONNX sessions.

    Ultralytics currently creates ``InferenceSession`` without session
    options, which makes ONNX Runtime allocate a host-sized pool in every
    Celery child. There is no public Ultralytics predictor setting for these
    options, so this process-local adapter supplies them only when the caller
    omitted them. Explicit options from any other caller always win.
    """
    try:
        import onnxruntime as ort
    except Exception as exc:
        log.debug("ONNX Runtime unavailable for thread budgeting: %s", exc)
        return False

    current = ort.InferenceSession
    if getattr(current, "_vivoguard_thread_bounded", False):
        return True

    def bounded_session(*args, **kwargs):
        if len(args) < 2 and "sess_options" not in kwargs:
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.add_session_config_entry(
                "session.intra_op.allow_spinning", "0"
            )
            options.add_session_config_entry(
                "session.inter_op.allow_spinning", "0"
            )
            kwargs["sess_options"] = options
        return current(*args, **kwargs)

    bounded_session._vivoguard_thread_bounded = True
    ort.InferenceSession = bounded_session
    log.info("ONNX Runtime session thread budget=%d; spinning disabled", threads)
    return True


def _package_version(name: str) -> str:
    """Installed version of a distribution, or 'none'."""
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "none"


def _cuda_arch() -> str:
    """'sm<major><minor>' of GPU 0 — engines are built per architecture."""
    try:
        import torch
        major, minor = torch.cuda.get_device_capability(0)
        return f"sm{major}{minor}"
    except Exception:
        return "smunknown"


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in value)


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
    def load_optimized(self, model_path: str, use_optimized: bool = True,
                       imgsz: int = 640):
        """Return (model, fmt).

        When use_optimized is True and the backend has an export format,
        export the model to that format once and load it through
        Ultralytics. The cached artifact lives under
        <models_dir>/optimized/<runtime key>/, where the key names
        everything a compiled artifact depends on (see _cache_key), so a
        container never loads an engine another runtime built.

        The optimized model is proven with one warm-up prediction before
        it is returned: Ultralytics may only deserialise a TensorRT engine
        on first use, and an incompatible engine then fails *inside
        predict()* — outside any try block here, on every frame. On ANY
        failure — and for rocm / use_optimized=False — this returns a raw
        PyTorch YOLO(model_path) and logs why, so detection keeps running.

        `fmt` is the format actually loaded ('pytorch', 'onnx',
        'engine', 'openvino', 'coreml')."""
        from ultralytics import YOLO   # heavy import — kept lazy

        spec_format = BACKEND_SPECS.get(self.backend, {}).get("export_format")

        # No-export paths: optimization off, rocm (raw HIP), or an
        # unknown backend.
        if not use_optimized or not spec_format:
            return self._load_pytorch(model_path), "pytorch"

        try:
            from app.config import settings
            base = Path(settings.models_dir) / "optimized"
            max_batch = max(1, int(getattr(settings, "inference_max_batch_size", 1)))
        except Exception as exc:
            log.debug("env_config: settings unavailable (%s); using defaults", exc)
            base = Path("data/models/optimized")
            max_batch = 1

        stem = Path(model_path).stem
        cache_dir = base / self._cache_key(stem, spec_format, max_batch, imgsz)

        failed = self._failure_marker(cache_dir)
        if failed:
            log.warning(
                "env_config: optimized %s for %s failed earlier in %s (%s) — "
                "using raw PyTorch. Delete that directory to retry the export.",
                spec_format, stem, cache_dir, failed)
            return self._load_pytorch(model_path), "pytorch"

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            artifact = self._cached_artifact(cache_dir, stem, spec_format)
            if artifact is None:
                artifact = self._export_locked(model_path, cache_dir, spec_format,
                                               max_batch, imgsz)
            t0 = time.perf_counter()
            model = YOLO(str(artifact))
            self._warm_up(model, imgsz)
            self.load_ms = (time.perf_counter() - t0) * 1000.0
            return model, spec_format
        except Exception as e:
            log.warning(
                "env_config: optimized %s load FAILED for %s (backend=%s, cache=%s): "
                "%s: %s — falling back to raw PyTorch; detection continues",
                spec_format, stem, self.backend, cache_dir, type(e).__name__, e)
            self._write_failure_marker(cache_dir, e)
            return self._load_pytorch(model_path), "pytorch"

    def _load_pytorch(self, model_path: str):
        from ultralytics import YOLO
        t0 = time.perf_counter()
        model = YOLO(model_path)
        self.load_ms = (time.perf_counter() - t0) * 1000.0
        return model

    def _warm_up(self, model, imgsz: int) -> None:
        """One prediction on a blank frame, so an artifact that cannot run
        here fails now (and falls back) rather than on the first camera
        frame."""
        import numpy as np
        frame = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
        model.predict(frame, imgsz=imgsz, device=self.device, verbose=False)

    # ---- cache key ------------------------------------------------------
    # A compiled TensorRT engine only runs under the TensorRT version, GPU
    # architecture, batch profile and input size it was built for, and
    # Ultralytics embeds its own metadata. All of those are in the
    # directory name, so containers built at different times (or with a
    # different batch/imgsz) each get their own artifact instead of
    # loading one they cannot deserialise.
    _FORMAT_RUNTIME = {"engine": "tensorrt", "onnx": "onnxruntime",
                       "openvino": "openvino", "coreml": "coremltools"}

    def _cache_key(self, stem: str, fmt: str, max_batch: int, imgsz: int) -> str:
        parts = [stem, self.backend]
        if max_batch > 1:
            parts.append(f"b{max_batch}")
        parts.append(f"i{imgsz}")
        parts.append("ul" + _package_version("ultralytics"))
        runtime = self._FORMAT_RUNTIME.get(fmt)
        if runtime:
            parts.append(runtime[:3] + _package_version(runtime))
        if self.backend == "cuda":
            parts.append(_cuda_arch())
        return _safe_name("_".join(parts))

    # ---- failure marker -------------------------------------------------
    _MARKER = "LOAD_FAILED.json"

    def _failure_marker(self, cache_dir: Path) -> Optional[str]:
        """The recorded reason if this artifact failed to load before."""
        marker = cache_dir / self._MARKER
        if not marker.exists():
            return None
        try:
            import json
            return str(json.loads(marker.read_text()).get("error", "unknown error"))
        except Exception as exc:
            return f"unreadable marker: {exc}"

    def _write_failure_marker(self, cache_dir: Path, error: Exception) -> None:
        """Remember the failure so every restart does not pay for (and log)
        the same failed load; the next runtime upgrade gets a new key."""
        try:
            import json
            from datetime import datetime, timezone
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / self._MARKER).write_text(json.dumps({
                "error": f"{type(error).__name__}: {error}"[:500],
                "at": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as exc:
            log.warning("env_config: could not record failure marker in %s: %s",
                        cache_dir, exc)

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

    def _export_locked(self, model_path: str, cache_dir: Path, fmt: str,
                       max_batch: int, imgsz: int) -> Path:
        """Export once even when several containers start together.

        Holds an exclusive lock on cache_dir while exporting; a process
        that waited re-checks for the finished artifact instead of
        exporting again."""
        import fcntl
        stem = Path(model_path).stem
        with open(cache_dir / ".export.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                done = self._cached_artifact(cache_dir, stem, fmt)
                if done is not None:
                    return done
                return self._export(model_path, cache_dir, fmt, max_batch, imgsz)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _export(self, model_path: str, cache_dir: Path, fmt: str,
                max_batch: int = 1, imgsz: int = 640) -> Path:
        """Export model_path to `fmt` and move the artifact into cache_dir.

        Ultralytics writes the export next to its source weights, so the
        weights are copied into a private temporary directory first: two
        processes exporting the same model can then never write the same
        file, and a half-written artifact never appears under its final
        name (the finished one is moved into place in one step)."""
        import shutil
        import tempfile
        from ultralytics import YOLO
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(dir=cache_dir, prefix=".export-") as tmp:
            src_copy = Path(tmp) / Path(model_path).name
            if Path(model_path).exists():
                shutil.copy2(model_path, src_copy)
                src = YOLO(str(src_copy))
            else:
                # A bare name such as "yolov8n.pt" that Ultralytics downloads.
                src = YOLO(model_path)
            kwargs: dict[str, Any] = {"format": fmt, "device": self.device,
                                      "imgsz": imgsz}
            if fmt == "engine":
                kwargs["batch"] = max_batch
                kwargs["dynamic"] = max_batch > 1
            produced = Path(src.export(**kwargs))
            target = cache_dir / produced.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            os.replace(produced, target)
        self.export_ms = (time.perf_counter() - t0) * 1000.0
        return target

    # ------------------------------------------------------------------
