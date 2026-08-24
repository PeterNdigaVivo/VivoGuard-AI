"""Measure safe YOLO batch sizes on the actual production GPU.

The benchmark is isolated from Redis, Postgres and camera streams. It uses
synthetic frames and exits non-zero if CUDA is unavailable, preventing a CPU
fallback from being mistaken for a successful capacity test.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import statistics
import time
from pathlib import Path

import numpy as np

# Support both the worker image (/app/scripts beside /app/app) and direct
# execution from a repository checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.env_config import HardwareEnv  # noqa: E402
from app.ai.yolov8_runner import infer_batch  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(
        0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    )
    return ordered[index]


def benchmark(*, batch_sizes: list[int], iterations: int, warmups: int,
              imgsz: int, weights: str, max_frame_p95_ms: float,
              max_vram_fraction: float) -> dict:
    import torch

    env = HardwareEnv.detect()
    if env.backend != "cuda" or not torch.cuda.is_available():
        raise RuntimeError(
            f"CUDA required; detected backend={env.backend!r}, device={env.device!r}"
        )
    total_vram = int(torch.cuda.get_device_properties(0).total_memory)
    frame = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    results = []

    for batch_size in batch_sizes:
        frames = [frame] * batch_size
        try:
            for _ in range(warmups):
                infer_batch(frames, weights=weights, imgsz=imgsz)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            timings = []
            for _ in range(iterations):
                started = time.perf_counter()
                infer_batch(frames, weights=weights, imgsz=imgsz)
                torch.cuda.synchronize()
                timings.append((time.perf_counter() - started) * 1000.0)
            peak_vram = int(torch.cuda.max_memory_allocated())
            p95_batch = _percentile(timings, 0.95)
            row = {
                "batch_size": batch_size,
                "p50_batch_ms": round(statistics.median(timings), 2),
                "p95_batch_ms": round(p95_batch, 2),
                "p95_per_frame_ms": round(p95_batch / batch_size, 2),
                "throughput_fps": round(
                    batch_size * 1000.0 / statistics.mean(timings), 2
                ),
                "peak_vram_mb": round(peak_vram / 1024 / 1024, 1),
                "peak_vram_fraction": round(peak_vram / total_vram, 4),
                "passed": (
                    p95_batch / batch_size <= max_frame_p95_ms
                    and peak_vram / total_vram <= max_vram_fraction
                ),
                "error": None,
            }
        except Exception as exc:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            row = {
                "batch_size": batch_size,
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(row)

    passing = [row["batch_size"] for row in results if row["passed"]]
    return {
        "backend": env.backend,
        "device": env.device,
        "gpu_name": env.gpu_name,
        "gpu_memory_mb": round(total_vram / 1024 / 1024, 1),
        "weights": weights,
        "imgsz": imgsz,
        "iterations": iterations,
        "acceptance": {
            "max_p95_per_frame_ms": max_frame_p95_ms,
            "max_vram_fraction": max_vram_fraction,
        },
        "results": results,
        "recommended_batch_size": max(passing) if passing else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-sizes", default="1,2,4,8,16,32")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--weights", default="yolov8n.pt")
    parser.add_argument("--max-frame-p95-ms", type=float, default=400.0)
    parser.add_argument("--max-vram-fraction", type=float, default=0.80)
    args = parser.parse_args()
    sizes = [int(value) for value in args.batch_sizes.split(",") if value.strip()]
    if not sizes or any(value < 1 for value in sizes):
        parser.error("--batch-sizes must contain positive integers")
    report = benchmark(
        batch_sizes=sizes,
        iterations=max(1, args.iterations),
        warmups=max(0, args.warmups),
        imgsz=args.imgsz,
        weights=args.weights,
        max_frame_p95_ms=args.max_frame_p95_ms,
        max_vram_fraction=args.max_vram_fraction,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["recommended_batch_size"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
