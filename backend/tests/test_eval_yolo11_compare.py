"""Task 2 tooling: per-model input size, clip people counts through the
live tracker + mannequin filter, PyTorch speed, pinned weight downloads."""
import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from app.ai import model_fetch, yolov8_runner
from app.ai.tracker import IOUTracker
from app.eval import clips, run, scoring


# ---- clips -------------------------------------------------------------------

def _det(x, y, w=0.2, h=0.4):
    return {"cls": "person", "conf": 0.9, "bbox_norm": [x, y, x + w, y + h]}


def _frames(n):
    return [np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(n)]


def test_mannequin_is_counted_raw_but_not_after_the_filter():
    step = iter(range(100))

    def infer(_frame):
        i = next(step)
        # One person walking right; one mannequin that never moves.
        return [_det(0.05 + 0.01 * i, 0.3), _det(0.7, 0.3)]

    got = clips.count_people(_frames(14), infer, IOUTracker(), window=10)
    assert (got.raw, got.filtered, got.frames) == (2, 1, 14)


def test_single_frame_flicker_is_not_a_person():
    step = iter(range(100))

    def infer(_frame):
        i = next(step)
        dets = [_det(0.05 + 0.01 * i, 0.3)]
        if i == 3:
            dets.append(_det(0.75, 0.1, 0.05, 0.05))   # one-frame false box
        return dets

    got = clips.count_people(_frames(6), infer, IOUTracker())
    assert got.raw == 1


def test_evaluate_clips_compares_with_the_hand_count():
    samples = [SimpleNamespace(id=1, clip_path="a.mp4", label={"count": 1}),
               SimpleNamespace(id=2, clip_path="b.mp4", label={"count": 3})]
    infer = lambda _f: [_det(0.1, 0.1)]                            # noqa: E731
    out = clips.evaluate_clips(samples, infer, fps=2,
                               frames_fn=lambda _p, _fps: _frames(4),
                               tracker_fn=lambda _fps: IOUTracker())
    assert out["raw"]["clips"] == 2
    assert out["raw"]["exact_pct"] == 50.0
    assert out["raw"]["mean_abs_error"] == 1.0
    assert out["errors"] == 0


def test_summarise_counts():
    s = scoring.summarise_counts([(2, 2), (3, 1), (0, 1), (4, 4)])
    assert s == {"clips": 4, "mean_abs_error": 0.75, "exact_pct": 50.0,
                 "within_one_pct": 75.0, "overcount_pct": 25.0, "undercount_pct": 25.0}
    assert scoring.summarise_counts([])["mean_abs_error"] is None


# ---- report -------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("yolo11m.pt@960", ("yolo11m.pt", 960)),
    ("yolo11m.pt", ("yolo11m.pt", 640)),
    ("/data/models/a@b/x.pt", ("/data/models/a@b/x.pt", 640)),
])
def test_parse_detector(spec, expected):
    assert run.parse_detector(spec, 640) == expected


def test_build_report_scores_each_model_at_its_own_size(monkeypatch):
    frame = SimpleNamespace(id=1, frame_path="f.jpg")
    samples = {"alerts": [], "uniform": [], "people": [], "clips": [frame]}
    monkeypatch.setattr(run, "load_samples", lambda db, limit=None: samples)
    monkeypatch.setattr(run, "snapshot_volume", lambda db: {})
    monkeypatch.setattr(run, "environment", lambda: {"gpu": None})
    monkeypatch.setattr(run, "evaluate_detector", lambda s, fn, conf: {
        "alerts": {}, "people": {}, "speed": {"median_ms": 1.0, "p95_ms": 2.0},
        "gpu_memory_mb": None, "errors": 0})
    built = []

    def factory(name, conf, imgsz):
        built.append((name, imgsz))
        fn = lambda f: []                                          # noqa: E731
        fn.model_format = "engine"
        return fn

    report = run.build_report(
        None, detectors=["yolo11m.pt", "yolo11m.pt@960"], uniform=[], conf=0.25, imgsz=640,
        infer_factory=factory, clips_fn=lambda s, fn, fps: {"raw": {"exact_pct": 50.0}},
        speed_fn=lambda *a, **k: {"median_ms": 9.0}, pytorch_speed_frames=0)

    assert built == [("yolo11m.pt", 640), ("yolo11m.pt", 960)]
    assert list(report["detectors"]) == ["yolo11m.pt", "yolo11m.pt@960"]
    assert report["detectors"]["yolo11m.pt@960"]["imgsz"] == 960
    assert report["detectors"]["yolo11m.pt"]["clips"]["raw"]["exact_pct"] == 50.0
    assert "speed_pytorch" not in report["detectors"]["yolo11m.pt"]   # 0 frames = skipped
    assert report["test_set"]["clips"] == 1


def test_render_tolerates_reports_from_before_these_columns():
    old = {"created_at": "x", "conf": 0.25, "imgsz": 640, "environment": {},
           "test_set": {"alerts": {}, "people_frames": 1, "people_clicked": 1},
           "detectors": {"yolov8n.pt": {
               "alerts": {}, "people": {"found_pct": 67.6, "people_missed": 354,
                                        "false_detections": 485, "edge_found_pct": 53.3},
               "speed": {"median_ms": 4.5, "p95_ms": 5.8}, "gpu_memory_mb": 338}},
           "uniform": {}}
    text = run.render(old, old)
    assert "clips: exact count % (raw) | —" in text
    assert "PyTorch speed median ms/frame | —" in text


# ---- loader -------------------------------------------------------------------

def test_load_model_keeps_one_model_per_input_size(monkeypatch):
    calls = []

    class Env:
        backend = device = "cpu"
        export_ms = load_ms = 0.0

        def load_optimized(self, weights, use_optimized, imgsz):
            calls.append(imgsz)
            return SimpleNamespace(names={0: "person"}), "onnx"

    monkeypatch.setattr(yolov8_runner, "_models", {})
    monkeypatch.setattr(yolov8_runner, "_hardware", lambda: Env())
    a = yolov8_runner.load_model("m.pt")
    b = yolov8_runner.load_model("m.pt", 960)
    assert a is not b
    assert yolov8_runner.load_model("m.pt") is a
    assert calls == [640, 960]


# ---- pinned weights ------------------------------------------------------------

def _manifest(data: bytes):
    return {"m.pt": {"url": "https://example.invalid/m.pt",
                     "sha256": hashlib.sha256(data).hexdigest()}}


def test_fetch_downloads_and_verifies(tmp_path):
    data = b"weights"
    path, action = model_fetch.fetch("m.pt", tmp_path, manifest=_manifest(data),
                                     download=lambda url, dest: dest.write_bytes(data))
    assert action == "downloaded" and path.read_bytes() == data
    _, again = model_fetch.fetch("m.pt", tmp_path, manifest=_manifest(data),
                                 download=lambda *_: pytest.fail("re-downloaded"))
    assert again == "present"


def test_fetch_rejects_a_bad_download_and_leaves_nothing(tmp_path):
    with pytest.raises(model_fetch.ChecksumMismatch):
        model_fetch.fetch("m.pt", tmp_path, manifest=_manifest(b"good"),
                          download=lambda url, dest: dest.write_bytes(b"tampered"))
    assert list(tmp_path.iterdir()) == []


def test_fetch_refuses_an_existing_file_with_the_wrong_checksum(tmp_path):
    (tmp_path / "m.pt").write_bytes(b"other")
    with pytest.raises(model_fetch.ChecksumMismatch):
        model_fetch.fetch("m.pt", tmp_path, manifest=_manifest(b"good"))


def test_manifest_pins_every_candidate_with_a_checksum():
    manifest = model_fetch.load_manifest()
    assert {"yolov8n.pt", "yolo11s.pt", "yolo11m.pt"} <= set(manifest)
    for name, entry in manifest.items():
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
        assert entry["url"].startswith("https://github.com/ultralytics/assets/releases/download/")
        assert entry["url"].endswith("/" + name)
