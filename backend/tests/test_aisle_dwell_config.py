from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_p2 import AisleDwellDetector


def _context(extra):
    return DetectorContext(
        camera_id=1,
        timestamp=0.0,
        raw_detections=[],
        tracks=[],
        zones=[{
            "id": 1,
            "name": "aisle",
            "polygon_coords_json": [],
            "detection_types_json": ["aisle"],
        }],
        config={"dwell": {"enabled": True, "extra": extra}},
        db=object(),
        store_id=1,
    )


def test_malformed_extra_uses_safe_defaults_without_crashing(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "app.analytics.recorder.record",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        AisleDwellDetector,
        "_publish_dwell_heatmap",
        lambda *args, **kwargs: None,
    )

    assert AisleDwellDetector().evaluate(_context([])) == []
    assert recorded


def test_malformed_detector_config_is_skipped_without_crashing():
    context = _context({})
    context.config["dwell"] = []

    assert AisleDwellDetector().evaluate(context) == []
