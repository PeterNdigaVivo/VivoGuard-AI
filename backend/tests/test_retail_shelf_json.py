"""Regression coverage for JSON-safe shelf-change detector output."""

import json

import numpy as np

from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_shelf import ShelfChangeDetector


def _context(frame: np.ndarray) -> DetectorContext:
    return DetectorContext(
        camera_id=1,
        timestamp=1.0,
        raw_detections=[],
        tracks=[],
        zones=[{
            "id": 10,
            "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "detection_types_json": ["shelf_change"],
        }],
        config={
            "shelf_change": {
                "enabled": True,
                "extra": {"baseline_seconds": 600, "divergence_threshold": 0.1},
            },
        },
        store_id=2,
        frame_bgr=frame,
    )


def test_baseline_and_event_payload_use_json_safe_floats(monkeypatch) -> None:
    detector = ShelfChangeDetector()
    clock = iter((1000.0, 1001.0, 1002.0))
    monkeypatch.setattr("app.ai.detectors.retail_shelf.time.time", lambda: next(clock))

    dark = np.zeros((8, 8, 3), dtype=np.uint8)
    detector.evaluate(_context(dark))
    detector.evaluate(_context(dark))

    state = detector._baselines[10]
    assert all(type(value) is float for value in state["hist"])
    state["locked"] = True

    bright = np.full((8, 8, 3), 255, dtype=np.uint8)
    events = detector.evaluate(_context(bright))

    assert len(events) == 1
    event = events[0]
    assert type(event.confidence) is float
    assert type(event.extra["chi_square"]) is float
    json.dumps({
        "confidence": event.confidence,
        "bbox": event.bbox_norm,
        "extra": event.extra,
    })
