import json
from dataclasses import asdict

import numpy as np

from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_shelf import ShelfChangeDetector


def test_shelf_change_event_payload_uses_native_json_scalars(monkeypatch) -> None:
    detector = ShelfChangeDetector()
    detector._baselines[7] = {
        "hist": [0.0] * 15 + [1.0],
        "count": 2,
        "since": 0.0,
        "locked": True,
        "fired_at": 0.0,
    }
    monkeypatch.setattr("app.ai.detectors.retail_shelf.time.time", lambda: 1000.0)

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
                    "divergence_threshold": 0.1,
                    "reset_after_change": False,
                },
            },
        },
        store_id=9,
        frame_bgr=np.zeros((8, 8, 3), dtype=np.uint8),
    )

    events = detector.evaluate(context)

    assert len(events) == 1
    event = events[0]
    assert type(event.confidence) is float
    assert type(event.extra["chi_square"]) is float
    assert type(event.extra["threshold"]) is float
    json.dumps(asdict(event))
