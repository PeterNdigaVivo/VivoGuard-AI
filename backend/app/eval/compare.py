"""Side-by-side comparison of detector candidates against the baseline.

    python -m app.eval.compare --report /data/eval/reports/report-<new>.json \\
        --baseline /data/eval/reports/report-20261117-182449.json

Reads an eval report holding the candidates (e.g. yolo11s.pt, yolo11m.pt,
yolo11m.pt@960) and the baseline report, checks every candidate against
the switch targets below, and writes compare-*.json + compare-*.md under
<eval_dir>/reports/ with a plain-language recommendation. Numbers only —
no images, names or alert ids.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.eval.categories import COUNTER_UNATTENDED, PERSON

log = logging.getLogger(__name__)

# What a candidate must reach to be worth switching (agreed targets).
# "Clearly fewer" false detections = at least 20% fewer than the baseline;
# "clearly higher" false alerts avoided = at least 10 percentage points up.
PEOPLE_FOUND_MIN = 80.0
EDGE_FOUND_MIN = 70.0
FALSE_DETECTIONS_MAX_RATIO = 0.80
COUNTER_FALSE_AVOIDED_GAIN = 10.0
COUNTER_REAL_KEPT_MIN = 95.0


def _get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def metrics(det: dict) -> dict:
    """The numbers the comparison is about, flattened."""
    return {
        "people_found_pct": _get(det, "people", "found_pct"),
        "edge_found_pct": _get(det, "people", "edge_found_pct"),
        "false_detections": _get(det, "people", "false_detections"),
        "people_missed": _get(det, "people", "people_missed"),
        "counter_real_kept_pct": _get(det, "alerts", COUNTER_UNATTENDED, "real_alerts_kept_pct"),
        "counter_false_avoided_pct": _get(det, "alerts", COUNTER_UNATTENDED, "false_alerts_avoided_pct"),
        "person_real_kept_pct": _get(det, "alerts", PERSON, "real_alerts_kept_pct"),
        "person_false_avoided_pct": _get(det, "alerts", PERSON, "false_alerts_avoided_pct"),
        "clips_exact_raw_pct": _get(det, "clips", "raw", "exact_pct"),
        "clips_exact_filtered_pct": _get(det, "clips", "filtered", "exact_pct"),
        "clips_off_by_filtered": _get(det, "clips", "filtered", "mean_abs_error"),
        "imgsz": det.get("imgsz"),
        "model_format": det.get("model_format"),
        "speed_median_ms": _get(det, "speed", "median_ms"),
        "speed_p95_ms": _get(det, "speed", "p95_ms"),
        "pytorch_speed_median_ms": _get(det, "speed_pytorch", "median_ms"),
        "gpu_memory_mb": det.get("gpu_memory_mb"),
    }


def check_targets(m: dict, base: dict) -> dict[str, dict]:
    """Each target → {"ok": bool|None, "need": str}. None = not measurable."""
    def at_least(v, minimum):
        return None if v is None else v >= minimum

    fd, base_fd = m["false_detections"], base["false_detections"]
    fd_max = None if base_fd is None else base_fd * FALSE_DETECTIONS_MAX_RATIO
    cf, base_cf = m["counter_false_avoided_pct"], base["counter_false_avoided_pct"]
    cf_min = None if base_cf is None else base_cf + COUNTER_FALSE_AVOIDED_GAIN
    return {
        "people_found": {"ok": at_least(m["people_found_pct"], PEOPLE_FOUND_MIN),
                         "need": f"≥ {PEOPLE_FOUND_MIN:g}%"},
        "edge_of_frame": {"ok": at_least(m["edge_found_pct"], EDGE_FOUND_MIN),
                          "need": f"≥ {EDGE_FOUND_MIN:g}%"},
        "false_detections": {"ok": None if fd is None or fd_max is None else fd <= fd_max,
                             "need": "—" if fd_max is None else f"≤ {fd_max:g} (20% fewer)"},
        "counter_false_avoided": {"ok": at_least(cf, cf_min) if cf_min is not None else None,
                                  "need": "—" if cf_min is None else f"≥ {cf_min:g}%"},
        "counter_real_kept": {"ok": at_least(m["counter_real_kept_pct"], COUNTER_REAL_KEPT_MIN),
                              "need": f"≥ {COUNTER_REAL_KEPT_MIN:g}%"},
    }


TARGET_WORDS = {
    "people_found": "finds at least 80% of people",
    "edge_of_frame": "finds at least 70% of people at the edge of the picture",
    "false_detections": "makes at least 20% fewer false detections",
    "counter_false_avoided": "avoids clearly more false counter-unattended alerts",
    "counter_real_kept": "keeps at least 95% of real counter-unattended alerts",
}


def recommend(candidates: dict[str, dict]) -> str:
    """Plain-language draft. Candidates meeting every target win, fastest
    first; otherwise name the closest and what it misses."""
    if not candidates:
        return "No candidates in the report."
    passing = {n: c for n, c in candidates.items()
               if all(t["ok"] for t in c["targets"].values())}
    if passing:
        best = min(passing, key=lambda n: passing[n]["metrics"]["speed_median_ms"] or 1e9)
        m = passing[best]["metrics"]
        others = [n for n in passing if n != best]
        text = (f"{best} meets every target: it finds {m['people_found_pct']}% of people "
                f"({m['edge_found_pct']}% at the picture's edge) and runs at "
                f"{m['speed_median_ms']} ms per frame ({m['model_format']}). "
                "Recommended for the shadow run on live cameras (Task 4).")
        if others:
            text += f" {', '.join(others)} also passes but is slower."
        return text

    def score(n):
        c = candidates[n]
        return (sum(bool(t["ok"]) for t in c["targets"].values()),
                c["metrics"]["people_found_pct"] or 0)
    best = max(candidates, key=score)
    missed = [TARGET_WORDS[k] for k, t in candidates[best]["targets"].items() if t["ok"] is False]
    unknown = [TARGET_WORDS[k] for k, t in candidates[best]["targets"].items() if t["ok"] is None]
    text = (f"No candidate meets every target. The closest is {best}, which still "
            f"misses: {'; '.join(missed) or 'nothing measurable'}.")
    if unknown:
        text += f" Not measurable from this report: {'; '.join(unknown)}."
    text += " Switching is not recommended on these numbers."
    return text


def build(report: dict, baseline: dict, baseline_name: str = "yolov8n.pt") -> dict:
    if baseline_name not in baseline.get("detectors", {}):
        raise SystemExit(f"{baseline_name} is not in the baseline report")
    base = metrics(baseline["detectors"][baseline_name])
    candidates = {}
    rerun = None
    for name, det in report.get("detectors", {}).items():
        m = metrics(det)
        if name == baseline_name:
            rerun = m           # same model re-measured (e.g. TensorRT, clips)
            continue
        candidates[name] = {"metrics": m, "targets": check_targets(m, base)}
    return {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "baseline": {"name": baseline_name, "created_at": baseline.get("created_at"),
                     "metrics": base},
        "baseline_rerun": rerun,
        "candidates": candidates,
        "report_created_at": report.get("created_at"),
        "environment": report.get("environment"),
        "recommendation": recommend(candidates),
    }


ROWS = [
    ("People found %", "people_found_pct", "people_found"),
    ("Edge-of-frame people found %", "edge_found_pct", "edge_of_frame"),
    ("False detections", "false_detections", "false_detections"),
    ("People missed", "people_missed", None),
    ("Counter unattended: real alerts kept %", "counter_real_kept_pct", "counter_real_kept"),
    ("Counter unattended: false alerts avoided %", "counter_false_avoided_pct", "counter_false_avoided"),
    ("Person: real alerts kept %", "person_real_kept_pct", None),
    ("Person: false alerts avoided %", "person_false_avoided_pct", None),
    ("Clips: exact people count % (raw)", "clips_exact_raw_pct", None),
    ("Clips: exact people count % (with mannequin filter)", "clips_exact_filtered_pct", None),
    ("Clips: count off by, mean (with mannequin filter)", "clips_off_by_filtered", None),
    ("Input size", "imgsz", None),
    ("Format (engine = TensorRT)", "model_format", None),
    ("Speed ms/frame, median", "speed_median_ms", None),
    ("Speed ms/frame, p95", "speed_p95_ms", None),
    ("PyTorch speed ms/frame, median", "pytorch_speed_median_ms", None),
    ("GPU memory MB", "gpu_memory_mb", None),
]


def _fmt(v) -> str:
    return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def render(cmp: dict) -> str:
    base = cmp["baseline"]
    cols = [(f"{base['name']} (baseline {base['created_at']})", base["metrics"], None)]
    if cmp["baseline_rerun"]:
        cols.append((f"{base['name']} (re-run)", cmp["baseline_rerun"], None))
    for name, c in cmp["candidates"].items():
        cols.append((name, c["metrics"], c["targets"]))
    lines = [f"# Detector comparison — {cmp['created_at']}", "",
             f"Candidates measured {cmp['report_created_at']} on "
             f"{(cmp.get('environment') or {}).get('gpu')}; ✅ meets the target, ❌ misses it.", "",
             "| | " + " | ".join(c[0] for c in cols) + " | Target |",
             "|---|" + "---|" * (len(cols) + 1)]
    for label, key, target in ROWS:
        cells, need = [], ""
        for _, m, targets in cols:
            cell = _fmt(m.get(key))
            if target and targets:
                ok = targets[target]["ok"]
                cell += "" if ok is None else (" ✅" if ok else " ❌")
                need = targets[target]["need"]
            cells.append(cell)
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {need} |")
    lines += ["", "Still frames are raw detector output scored with the live alert rule "
              "(person inside the zone). Clip counts run the live tracker and, in the "
              "filtered row, the live mannequin filter, which needs several frames of "
              "movement and so cannot be applied to still frames.", "",
              "## Recommendation", "", cmp["recommendation"], ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from app.config import settings
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path, required=True, help="eval report with the candidates")
    p.add_argument("--baseline", type=Path, required=True, help="the baseline eval report")
    p.add_argument("--baseline-name", default="yolov8n.pt")
    p.add_argument("--out", type=Path, default=Path(settings.eval_dir) / "reports")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmp = build(json.loads(args.report.read_text()), json.loads(args.baseline.read_text()),
                args.baseline_name)
    text = render(cmp)
    from app.eval.collect import ensure_private_dir, private_write_text
    ensure_private_dir(args.out)
    stem = args.out / datetime.now(timezone.utc).strftime("compare-%Y%m%d-%H%M%S")
    private_write_text(stem.with_suffix(".json"), json.dumps(cmp, indent=2, default=str))
    private_write_text(stem.with_suffix(".md"), text)
    print(text)
    print(f"Saved {stem}.json and {stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
