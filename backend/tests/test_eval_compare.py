"""app.eval.compare: targets, recommendation, rendering."""
from app.eval import compare
from app.eval.categories import COUNTER_UNATTENDED, PERSON


def _det(found, edge, false_dets, counter_kept, counter_avoided, ms, fmt="engine", imgsz=640):
    return {"people": {"found_pct": found, "edge_found_pct": edge,
                       "false_detections": false_dets, "people_missed": 100},
            "alerts": {COUNTER_UNATTENDED: {"real_alerts_kept_pct": counter_kept,
                                            "false_alerts_avoided_pct": counter_avoided},
                       PERSON: {"real_alerts_kept_pct": 61.1, "false_alerts_avoided_pct": 66.5}},
            "speed": {"median_ms": ms, "p95_ms": ms + 1}, "gpu_memory_mb": 400,
            "model_format": fmt, "imgsz": imgsz}


# The real baseline: report-20261117-182449.json.
BASELINE = {"created_at": "2026-11-17 18:24 UTC",
            "detectors": {"yolov8n.pt": _det(67.6, 53.3, 485, 95.1, 15.6, 4.5, "pytorch")}}


def test_targets_are_checked_against_the_baseline():
    base = compare.metrics(BASELINE["detectors"]["yolov8n.pt"])
    good = compare.check_targets(compare.metrics(_det(82, 71, 388, 95.0, 25.6, 9)), base)
    assert all(t["ok"] for t in good.values())
    bad = compare.check_targets(compare.metrics(_det(79.9, 70, 389, 94.9, 25.5, 9)), base)
    assert {k for k, t in bad.items() if not t["ok"]} == {
        "people_found", "false_detections", "counter_real_kept", "counter_false_avoided"}
    assert bad["false_detections"]["need"] == "≤ 388 (20% fewer)"


def test_fastest_passing_candidate_is_recommended():
    report = {"created_at": "now", "detectors": {
        "yolov8n.pt": _det(67, 53, 480, 95, 16, 2.0),
        "yolo11s.pt": _det(81, 72, 300, 96, 30, 3.0),
        "yolo11m.pt": _det(85, 75, 250, 97, 35, 5.0)}}
    cmp = compare.build(report, BASELINE)
    assert cmp["baseline_rerun"]["speed_median_ms"] == 2.0
    assert list(cmp["candidates"]) == ["yolo11s.pt", "yolo11m.pt"]
    assert cmp["recommendation"].startswith("yolo11s.pt meets every target")
    assert "yolo11m.pt also passes but is slower" in cmp["recommendation"]


def test_no_switch_when_nothing_passes_and_the_gap_is_named():
    report = {"created_at": "now", "detectors": {
        "yolo11m.pt": _det(78, 72, 300, 96, 30, 5.0),
        "yolo11m.pt@960": _det(83, 76, 420, 96, 20, 9.0, imgsz=960)}}
    cmp = compare.build(report, BASELINE)
    text = cmp["recommendation"]
    assert text.startswith("No candidate meets every target. The closest is yolo11m.pt,")
    assert "finds at least 80% of people" in text
    assert "Switching is not recommended" in text


def test_render_shows_all_columns_and_ticks():
    report = {"created_at": "now", "environment": {"gpu": "RTX"},
              "detectors": {"yolo11m.pt": _det(85, 75, 250, 97, 35, 5.0)}}
    md = compare.render(compare.build(report, BASELINE))
    assert "| yolov8n.pt (baseline 2026-11-17 18:24 UTC) | yolo11m.pt | Target |" in md
    assert "| People found % | 67.6 | 85 ✅ | ≥ 80% |" in md
    assert "## Recommendation" in md
