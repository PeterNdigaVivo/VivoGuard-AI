"""Measure GPU headroom over the busiest hours.

    python -m app.eval.gpu_sample --hours 8 --interval 30

Samples GPU utilisation, memory and per-process memory through NVML,
then prints peak / typical load and free memory, overall and per
Nairobi hour, so we know which extra models can fit. Ctrl-C stops early
and still writes the report (<eval_dir>/reports/gpu-*.json).
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
EAT = ZoneInfo("Africa/Nairobi")


def read_sample(pynvml, handle) -> dict:
    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
    procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
    return {
        "ts": time.time(),
        "gpu_util_pct": int(util.gpu),
        "mem_used_mb": round(mem.used / 2**20),
        "mem_total_mb": round(mem.total / 2**20),
        "processes": len(procs),
        "process_mem_mb": sorted((round((p.usedGpuMemory or 0) / 2**20) for p in procs),
                                 reverse=True),
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def summarise(samples: list[dict]) -> dict:
    """Peak / p95 / median load, free memory, and the busiest hour."""
    if not samples:
        return {"samples": 0}
    util = [s["gpu_util_pct"] for s in samples]
    used = [s["mem_used_mb"] for s in samples]
    total = samples[0]["mem_total_mb"]
    by_hour: dict[int, list[int]] = {}
    for s in samples:
        hour = datetime.fromtimestamp(s["ts"], EAT).hour
        by_hour.setdefault(hour, []).append(s["gpu_util_pct"])
    hours = {f"{h:02d}:00": round(statistics.mean(v), 1) for h, v in sorted(by_hour.items())}
    busiest = max(hours, key=hours.get)
    return {
        "samples": len(samples),
        "gpu_util_peak_pct": max(util),
        "gpu_util_p95_pct": _p95(util),
        "gpu_util_median_pct": statistics.median(util),
        "mem_total_mb": total,
        "mem_used_peak_mb": max(used),
        "mem_free_min_mb": total - max(used),
        "busiest_hour_eat": busiest,
        "busiest_hour_util_mean_pct": hours[busiest],
        "util_mean_by_hour_eat": hours,
        "spare_compute_at_p95_pct": max(0, 100 - _p95(util)),
    }


def render(summary: dict) -> str:
    if not summary.get("samples"):
        return "No GPU samples collected.\n"
    return (
        f"GPU samples: {summary['samples']}\n"
        f"Utilisation: peak {summary['gpu_util_peak_pct']}%, 95th percentile "
        f"{summary['gpu_util_p95_pct']}%, median {summary['gpu_util_median_pct']}%\n"
        f"Busiest hour (Nairobi): {summary['busiest_hour_eat']} "
        f"(mean {summary['busiest_hour_util_mean_pct']}%)\n"
        f"Memory: peak used {summary['mem_used_peak_mb']} of {summary['mem_total_mb']} MB "
        f"-> at least {summary['mem_free_min_mb']} MB free\n"
        f"Spare compute at the 95th percentile: {summary['spare_compute_at_p95_pct']}%\n"
    )


def main(argv: list[str] | None = None) -> int:
    from app.config import settings
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=8.0)
    p.add_argument("--interval", type=float, default=30.0, help="seconds between samples")
    p.add_argument("--device", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path(settings.eval_dir) / "reports")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception as exc:                                    # noqa: BLE001
        print(f"NVML is not available here (no NVIDIA GPU or driver): {exc}", file=sys.stderr)
        return 2
    samples: list[dict] = []
    deadline = time.time() + args.hours * 3600
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(args.device)
        print(f"Sampling GPU {args.device} every {args.interval:g}s for {args.hours:g}h "
              "(Ctrl-C to stop early)...", flush=True)
        while time.time() < deadline:
            try:
                samples.append(read_sample(pynvml, handle))
            except pynvml.NVMLError as exc:
                log.warning("GPU sample failed: %s", exc)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped early.", flush=True)
    finally:
        pynvml.nvmlShutdown()
    summary = summarise(samples)
    from app.eval.collect import ensure_private_dir, private_write_text
    ensure_private_dir(args.out.parent)
    ensure_private_dir(args.out)
    path = args.out / datetime.now(timezone.utc).strftime("gpu-%Y%m%d-%H%M%S.json")
    private_write_text(path, json.dumps({"summary": summary, "samples": samples}, indent=1))
    print(render(summary))
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
