"""Compatibility tests for legacy retail detector configuration JSON."""

from app.ai.detectors import retail_p2
from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_p2 import AisleDwellDetector, _config_extra


def test_config_extra_returns_mapping_overrides() -> None:
    extra = {"unattended_customers": 4}

    assert _config_extra({"extra": extra}) == extra


def test_config_extra_ignores_legacy_list_shape() -> None:
    assert _config_extra({"extra": []}) == {}


def test_config_extra_ignores_missing_or_null_shape() -> None:
    assert _config_extra({}) == {}
    assert _config_extra({"extra": None}) == {}


def test_aisle_dwell_legacy_list_extra_uses_safe_defaults(monkeypatch) -> None:
    clock = type("Clock", (), {"time": staticmethod(lambda: 1000.0)})
    monkeypatch.setattr(retail_p2, "time", clock)
    from app.analytics import recorder

    monkeypatch.setattr(recorder, "record", lambda *args, **kwargs: None)
    detector = AisleDwellDetector()
    ctx = DetectorContext(
        camera_id=1,
        timestamp=1000.0,
        raw_detections=[],
        tracks=[],
        zones=[{
            "id": 10,
            "name": "Aisle",
            "polygon_coords_json": [],
            "detection_types_json": ["aisle"],
        }],
        config={"dwell": {"enabled": True, "extra": []}},
        db=object(),
    )

    assert detector.evaluate(ctx) == []
