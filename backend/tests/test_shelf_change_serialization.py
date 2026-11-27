import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import numpy as np

from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_shelf import ShelfChangeDetector


def test_shelf_change_serializes_after_accumulating_baseline(monkeypatch) -> None:
    detector = ShelfChangeDetector()
    ticks = iter((1000.0, 1002.0, 1003.0))
    monkeypatch.setattr(
        "app.ai.detectors.retail_shelf.time", SimpleNamespace(time=lambda: next(ticks)),
    )

    context = DetectorContext(
        camera_id=1,
        timestamp=1000.0,
        raw_detections=[],
        tracks=[],
        zones=[{
            "id": 7,
            "polygon_coords_json": [
                [0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0],
            ],
            "detection_types_json": ["shelf_change"],
        }],
        config={
            "shelf_change": {
                "enabled": True,
                "extra": {
                    "baseline_seconds": 1,
                    "divergence_threshold": 0.1,
                    "reset_after_change": False,
                },
            },
        },
        store_id=9,
        frame_bgr=np.full((8, 8, 3), 255, dtype=np.uint8),
    )

    assert detector.evaluate(context) == []
    assert detector.evaluate(replace(context, timestamp=1002.0)) == []
    events = detector.evaluate(replace(
        context, timestamp=1003.0, frame_bgr=np.zeros_like(context.frame_bgr),
    ))

    assert len(events) == 1
    payload = json.loads(json.dumps(asdict(events[0]), allow_nan=False))
    assert payload["confidence"] == 1.0
    assert payload["extra"] == {"chi_square": 1.0, "threshold": 0.1, "store_id": 9}
