"""Score detectors / uniform methods on the evaluation test set.

    python -m app.eval.run --detector yolov8n.pt
    python -m app.eval.run --detector yolov8n.pt --detector yolo11m.pt
    python -m app.eval.run --detector yolov8n.pt --compare /data/eval/reports/<old>.json

Run inside a GPU worker container (worker-training is usually idle):
it uses the same loader and TensorRT path as live inference. Prints a
plain report and saves it as JSON + Markdown under <eval_dir>/reports/.
Reports hold numbers only — no images, names or alert ids.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.eval import scoring
from app.eval.categories import COUNTER_UNATTENDED, PERSON, UNIFORM

log = logging.getLogger(__name__)

WARMUP_RUNS = 3
InferFn = Callable[[object], list[dict]]


# ---- test set ----------------------------------------------------------------

def load_samples(db, limit: int | None = None) -> dict[str, list]:
    """Labelled samples whose files still exist, grouped by use."""
    from app.models import EvalSample
    q = db.query(EvalSample).filter(EvalSample.status == "labelled")
    out: dict[str, list] = {"alerts": [], "uniform": [], "people": []}
    for s in q.order_by(EvalSample.id):
        if not s.frame_path or not os.path.isfile(s.frame_path):
            continue
        if s.kind == "alert" and s.category in (COUNTER_UNATTENDED, PERSON):
            out["alerts"].append(s)
        elif s.kind == "alert" and s.category == UNIFORM and s.event_bbox:
            out["uniform"].append(s)
        elif s.kind == "people" and (s.label or {}).get("points") is not None:
            out["people"].append(s)
    if limit:
        out = {k: v[:limit] for k, v in out.items()}
    return out


def _read(path: str):
    import cv2
    frame = cv2.imread(path)
    if frame is None:
        raise OSError(f"cannot read image {path}")
    return frame


# ---- GPU memory -----------------------------------------------------------------

def gpu_process_memory_mb() -> float | None:
    """GPU memory used by THIS process (NVML), None without a GPU."""
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            total = 0
            for i in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    if proc.pid == os.getpid() and proc.usedGpuMemory:
                        total += proc.usedGpuMemory
            return round(total / 2**20, 1)
        finally:
            pynvml.nvmlShutdown()
    except Exception as exc:                                    # noqa: BLE001
        log.debug("GPU memory unavailable: %s", exc)
        return None


# ---- evaluators -----------------------------------------------------------------

def evaluate_detector(samples: dict[str, list], infer_fn: InferFn, *,
                      conf: float, read=_read, memory_fn=gpu_process_memory_mb) -> dict:
    """Run one detector over the alert + people samples."""
    frames = [s.frame_path for s in samples["alerts"] + samples["people"]]
    mem_before = memory_fn()
    if frames:
        warm = read(frames[0])
        for _ in range(WARMUP_RUNS):
            infer_fn(warm)
    mem_after = memory_fn()
    timings: list[float] = []
    outcomes: list[tuple[str, str]] = []
    matches: list[dict] = []
    errors = 0

    def run(path):
        frame = read(path)
        t0 = time.perf_counter()
        dets = infer_fn(frame)
        timings.append((time.perf_counter() - t0) * 1000)
        return scoring.person_boxes(dets, conf)

    for s in samples["alerts"]:
        try:
            boxes = run(s.frame_path)
        except Exception as exc:                                # noqa: BLE001
            errors += 1
            log.warning("eval: sample %s failed: %s", s.id, exc)
            continue
        present = scoring.person_in_zone(boxes, s.zone_polygon,
                                         generous=s.category == COUNTER_UNATTENDED)
        outcomes.append((s.category, scoring.alert_outcome(
            s.verdict, scoring.model_raises_alert(s.category, person_present=present))))
    for s in samples["people"]:
        try:
            boxes = run(s.frame_path)
        except Exception as exc:                                # noqa: BLE001
            errors += 1
            log.warning("eval: sample %s failed: %s", s.id, exc)
            continue
        matches.append(scoring.match_clicks(boxes, s.label["points"]))
    return {
        "alerts": scoring.summarise_alerts(outcomes),
        "people": scoring.summarise_people(matches),
        "speed": scoring.summarise_speed(timings),
        "gpu_memory_mb": (round(mem_after - mem_before, 1)
                          if mem_before is not None and mem_after is not None else None),
        "errors": errors,
    }


def uniform_rules_state(frame, bbox_norm) -> str:
    """The live rule path: orange-lanyard evidence, else the colour tree."""
    from app.ai.detectors.uniform_compliance import (
        FULL_COMPLIANT, classify_by_rules, has_orange_lanyard_evidence, uniform_features,
    )
    feats = uniform_features(frame, bbox_norm)
    if has_orange_lanyard_evidence(feats):
        return FULL_COMPLIANT
    return classify_by_rules(feats)


UNIFORM_METHODS: dict[str, Callable] = {"rules": uniform_rules_state}


def evaluate_uniform(samples: list, method: Callable, *, read=_read) -> dict:
    timings: list[float] = []
    outcomes: list[tuple[str, str]] = []
    errors = 0
    for s in samples:
        try:
            frame = read(s.frame_path)
            t0 = time.perf_counter()
            state = method(frame, s.event_bbox)
            timings.append((time.perf_counter() - t0) * 1000)
        except Exception as exc:                                # noqa: BLE001
            errors += 1
            log.warning("eval: uniform sample %s failed: %s", s.id, exc)
            continue
        outcomes.append((UNIFORM, scoring.alert_outcome(
            s.verdict, scoring.model_raises_alert(UNIFORM, uniform_state=state))))
    return {"alerts": scoring.summarise_alerts(outcomes),
            "speed": scoring.summarise_speed(timings), "errors": errors}


# ---- context -----------------------------------------------------------------------

def environment() -> dict:
    """Versions + GPU, so a report says exactly what was measured (and the
    exact versions to pin)."""
    from importlib import metadata
    env = {"python": platform.python_version()}
    for pkg in ("ultralytics", "torch", "torchvision", "tensorrt", "onnxruntime-gpu",
                "opencv-python-headless", "supervision", "numpy"):
        try:
            env[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            env[pkg] = None
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        env["gpu"] = name.decode() if isinstance(name, bytes) else name
        env["gpu_memory_total_mb"] = round(pynvml.nvmlDeviceGetMemoryInfo(handle).total / 2**20)
        env["nvidia_driver"] = pynvml.nvmlSystemGetDriverVersion()
        pynvml.nvmlShutdown()
    except Exception as exc:                                    # noqa: BLE001
        env["gpu"] = None
        log.debug("no GPU info: %s", exc)
    return env


def snapshot_volume(db, days: int = 30) -> dict:
    """Alert snapshots per day (sizes the Task 4 embedding store)."""
    from sqlalchemy import func

    from app.models import DetectionEvent
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(func.date(DetectionEvent.timestamp), func.count(DetectionEvent.id))
              .filter(DetectionEvent.thumbnail_path.isnot(None),
                      DetectionEvent.timestamp >= since)
              .group_by(func.date(DetectionEvent.timestamp)).all())
    counts = [n for _, n in rows]
    return {"days": days, "median_per_day": statistics.median(counts) if counts else 0,
            "max_per_day": max(counts) if counts else 0, "total": sum(counts)}


def test_set_size(samples: dict[str, list]) -> dict:
    sizes: dict[str, dict] = {}
    for s in samples["alerts"] + samples["uniform"]:
        row = sizes.setdefault(s.category, {"true": 0, "false": 0})
        row[s.verdict] = row.get(s.verdict, 0) + 1
    return {"alerts": sizes, "people_frames": len(samples["people"]),
            "people_clicked": sum(len(s.label["points"]) for s in samples["people"])}


# ---- rendering -----------------------------------------------------------------------

def _fmt(v) -> str:
    return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def _pct(v) -> str:
    return "—" if v is None else f"{v:g}%"


def render(report: dict, previous: dict | None = None) -> str:
    """Plain-text / Markdown tables, one column per model; with
    `previous`, also the earlier report's value for each row."""
    lines = [f"# Model evaluation report — {report['created_at']}", ""]
    size = report["test_set"]
    lines.append(f"Test set: {json.dumps(size['alerts'])}; people frames "
                 f"{size['people_frames']} ({size['people_clicked']} people clicked)")
    lines.append(f"Settings: conf={report['conf']} imgsz={report['imgsz']}; "
                 f"GPU={report['environment'].get('gpu')}")
    lines.append("")
    names = list(report["detectors"])
    if names:
        lines.append("| Detector | " + " | ".join(names) + " |")
        lines.append("|---|" + "---|" * len(names))

        def row(label, getter):
            vals = []
            for n in names:
                v = _fmt(getter(report["detectors"][n]))
                if previous and n in previous.get("detectors", {}):
                    v += f" (was {_fmt(getter(previous['detectors'][n]))})"
                vals.append(v)
            lines.append(f"| {label} | " + " | ".join(vals) + " |")

        for cat in (COUNTER_UNATTENDED, PERSON):
            row(f"{cat}: real alerts kept %", lambda d, c=cat: d["alerts"].get(c, {}).get("real_alerts_kept_pct"))
            row(f"{cat}: false alerts avoided %", lambda d, c=cat: d["alerts"].get(c, {}).get("false_alerts_avoided_pct"))
        row("people found %", lambda d: d["people"]["found_pct"])
        row("people missed", lambda d: d["people"]["people_missed"])
        row("false detections", lambda d: d["people"]["false_detections"])
        row("edge-of-frame people found %", lambda d: d["people"]["edge_found_pct"])
        row("speed median ms/frame", lambda d: d["speed"]["median_ms"])
        row("speed p95 ms/frame", lambda d: d["speed"]["p95_ms"])
        row("GPU memory MB", lambda d: d["gpu_memory_mb"])
        lines.append("")
    for name, res in report["uniform"].items():
        u = res["alerts"].get(UNIFORM, {})
        lines.append(f"Uniform ({name}): real alerts kept {_pct(u.get('real_alerts_kept_pct'))}, "
                     f"false alerts avoided {_pct(u.get('false_alerts_avoided_pct'))}, "
                     f"{_fmt(res['speed']['median_ms'])} ms/crop")
    vol = report.get("snapshot_volume") or {}
    if vol:
        lines.append(f"Alert snapshots/day (last {vol['days']} days): median "
                     f"{_fmt(vol['median_per_day'])}, max {_fmt(vol['max_per_day'])}")
    return "\n".join(lines) + "\n"


# ---- entry point -----------------------------------------------------------------------

def _detector_infer_fn(name: str, conf: float, imgsz: int) -> InferFn:
    from app.ai.yolov8_runner import infer, load_model, resolve_weights
    weights = resolve_weights(name)
    load_model(weights)
    return lambda frame: infer(frame, weights=weights, conf=conf, imgsz=imgsz)


def build_report(db, *, detectors: list[str], uniform: list[str], conf: float,
                 imgsz: int, limit: int | None = None,
                 infer_factory=_detector_infer_fn) -> dict:
    samples = load_samples(db, limit)
    report = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "conf": conf, "imgsz": imgsz, "environment": environment(),
        "test_set": test_set_size(samples), "detectors": {}, "uniform": {},
        "snapshot_volume": snapshot_volume(db),
    }
    for name in detectors:
        log.info("eval: scoring detector %s", name)
        report["detectors"][name] = evaluate_detector(
            samples, infer_factory(name, conf, imgsz), conf=conf)
    for name in uniform:
        report["uniform"][name] = evaluate_uniform(samples["uniform"], UNIFORM_METHODS[name])
    return report


def main(argv: list[str] | None = None) -> int:
    from app.config import settings
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--detector", action="append", default=[],
                   help="weights name or path (repeat to compare)")
    p.add_argument("--uniform", action="append", default=[], choices=sorted(UNIFORM_METHODS))
    p.add_argument("--conf", type=float, default=0.25, help="same default as live inference")
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--limit", type=int, default=None, help="max samples per group (quick runs)")
    p.add_argument("--compare", type=Path, default=None, help="an earlier report .json")
    p.add_argument("--out", type=Path, default=Path(settings.eval_dir) / "reports")
    args = p.parse_args(argv)
    if not args.detector and not args.uniform:
        args.detector, args.uniform = [settings.default_model], ["rules"]
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from app.database import SessionLocal
    with SessionLocal() as db:
        report = build_report(db, detectors=args.detector, uniform=args.uniform,
                              conf=args.conf, imgsz=args.imgsz, limit=args.limit)
    previous = json.loads(args.compare.read_text()) if args.compare else None
    text = render(report, previous)
    from app.eval.collect import ensure_private_dir, private_write_text
    ensure_private_dir(args.out.parent)
    ensure_private_dir(args.out)
    stem = args.out / datetime.now(timezone.utc).strftime("report-%Y%m%d-%H%M%S")
    private_write_text(stem.with_suffix(".json"), json.dumps(report, indent=2, default=str))
    private_write_text(stem.with_suffix(".md"), text)
    print(text)
    print(f"Saved {stem}.json and {stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
