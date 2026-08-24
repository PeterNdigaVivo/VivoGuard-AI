import io
import json
import statistics

import numpy as np
from PIL import Image

from app.ai import batch_coordinator as coordinator_module
from app.ai.batch_coordinator import (
    BatchCandidate,
    BatchShadowCoordinator,
    WeightedFairBatchScheduler,
    risk_priority,
)


def _candidate(camera_id: int, *, weights: str = "model.pt",
               priority: int = 1) -> BatchCandidate:
    return BatchCandidate(
        camera_id=camera_id,
        frame_ts=float(camera_id),
        frame=np.zeros((16, 16, 3), dtype=np.uint8),
        weights=weights,
        priority=priority,
    )


class _Redis:
    def __init__(self):
        self.values = {}
        self.set_calls = 0

    def set(self, key, value, *, ex):
        self.set_calls += 1
        self.values[key] = (json.loads(value), ex)


class _Buffer:
    def __init__(self):
        image = Image.new("RGB", (8, 8), "black")
        payload = io.BytesIO()
        image.save(payload, format="JPEG")
        self.jpeg = payload.getvalue()
        self.reads = []

    def health(self, camera_id):
        return {"last_frame_at": float(camera_id)}

    def latest_jpeg(self, camera_id, *, prefer_overlay):
        assert prefer_overlay is False
        self.reads.append(camera_id)
        return self.jpeg


def test_risk_priority_is_bounded_and_detector_driven():
    assert risk_priority({}) == 1
    assert risk_priority({"checkout_dwell": {"enabled": True}}) == 2
    assert risk_priority({"weapon": {"enabled": True}}) == 3
    assert risk_priority({"weapon": {"enabled": False}}) == 1


def test_scheduler_never_mixes_model_weights_in_one_batch():
    scheduler = WeightedFairBatchScheduler()
    selected = scheduler.select(
        [_candidate(1, weights="a.pt"), _candidate(2, weights="b.pt")],
        batch_size=8,
        now=10.0,
    )
    assert len({candidate.weights for candidate in selected}) == 1


def test_priority_breaks_initial_tie_but_elapsed_wait_prevents_starvation():
    scheduler = WeightedFairBatchScheduler()
    candidates = [_candidate(1, priority=1), _candidate(2, priority=3)]

    assert scheduler.select(candidates, batch_size=1, now=10.0)[0].camera_id == 2
    assert scheduler.select(candidates, batch_size=1, now=10.1)[0].camera_id == 1


def test_replay_scheduler_covers_110_cameras_fairly():
    scheduler = WeightedFairBatchScheduler()
    candidates = [
        _candidate(camera_id, priority=3 if camera_id % 11 == 0 else 1)
        for camera_id in range(1, 111)
    ]
    counts = {candidate.camera_id: 0 for candidate in candidates}
    now = 1_000.0
    for _ in range(140):
        selected = scheduler.select(candidates, batch_size=8, now=now)
        for candidate in selected:
            counts[candidate.camera_id] += 1
        now += 0.05

    low_risk = [count for camera_id, count in counts.items() if camera_id % 11]
    critical = [count for camera_id, count in counts.items() if camera_id % 11 == 0]
    assert min(counts.values()) >= 8
    assert statistics.mean(critical) > statistics.mean(low_risk) * 2
    assert statistics.mean(critical) < statistics.mean(low_risk) * 4


def test_coordinator_decodes_only_the_selected_bounded_batch():
    coordinator = BatchShadowCoordinator()
    coordinator.buffer = _Buffer()
    coordinator.specs = {
        camera_id: ("model.pt", 1) for camera_id in range(1, 111)
    }

    candidates = coordinator.candidates()
    assert len(candidates) == 110
    assert coordinator.buffer.reads == []
    selected = coordinator.scheduler.select(
        candidates, batch_size=8, now=100.0,
    )
    decoded = coordinator.decode_selected(selected)
    assert len(decoded) == 8
    assert len(coordinator.buffer.reads) == 8
    assert all(candidate.frame is not None for candidate in decoded)


def test_shadow_process_records_telemetry_without_emitting_results(monkeypatch):
    coordinator = BatchShadowCoordinator()
    coordinator.redis = _Redis()
    coordinator.specs = {1: ("model.pt", 3), 2: ("model.pt", 1)}
    coordinator.last_refresh = 100.0
    candidates = [_candidate(1, priority=3), _candidate(2)]
    monkeypatch.setattr(coordinator, "candidates", lambda: candidates)
    monkeypatch.setattr(coordinator, "decode_selected", lambda selected: selected)
    calls = []

    def fake_infer(frames, *, weights, conf):
        calls.append((len(frames), weights, conf))
        return [[{"cls": "person"}], []]

    monkeypatch.setattr(coordinator_module, "infer_batch", fake_infer)

    assert coordinator.process_once(now=100.0) == 2
    assert calls == [(2, "model.pt", 0.25)]
    payload, ttl = coordinator.redis.values[coordinator_module.HEALTH_KEY]
    assert payload["mode"] == "shadow"
    assert payload["authoritative"] is False
    assert payload["frames_processed"] == 2
    assert payload["cameras_served"] == 2
    assert payload["detections_observed_not_emitted"] == 1
    assert payload["p95_per_frame_ms"] is not None
    assert ttl == 120
    coordinator.write_health(now=100.5, candidates=0, detections=0)
    assert coordinator.redis.set_calls == 1


def test_shadow_failure_does_not_mark_frames_processed(monkeypatch):
    coordinator = BatchShadowCoordinator()
    coordinator.redis = _Redis()
    coordinator.specs = {1: ("model.pt", 1)}
    coordinator.last_refresh = 100.0
    monkeypatch.setattr(coordinator, "candidates", lambda: [_candidate(1)])
    monkeypatch.setattr(coordinator, "decode_selected", lambda selected: selected)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic GPU failure")

    monkeypatch.setattr(coordinator_module, "infer_batch", fail)
    assert coordinator.process_once(now=100.0) == 0
    assert coordinator.last_processed_ts == {}
    assert coordinator.errors == 1
