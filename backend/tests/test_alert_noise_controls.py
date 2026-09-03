"""Regression tests for controls that prevent frame noise becoming alerts."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.ai.detectors.base import DetectorContext
from app.ai.detectors.simple import PersonDetector
from app.ai.inference_worker import (_alert_dedup_key, _large_enough,
                                     _schedule_active)


def test_stateless_detector_preserves_tracker_identity() -> None:
    det = {"cls": "person", "conf": 0.91, "rolling_conf": 0.84,
           "bbox_norm": [0.1, 0.1, 0.4, 0.8],
           "bbox_px": [10, 10, 40, 80], "track_id": 42}
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[det], tracks=[], zones=[],
        config={"person": {"enabled": True, "confidence_threshold": 0.6}},
    )
    event = PersonDetector().evaluate(ctx)[0]
    assert event.track_id == 42
    assert event.confidence == 0.84


def test_incident_key_uses_track_when_available() -> None:
    event = SimpleNamespace(detection_type="person", zone_id=3, track_id=42,
                            bbox_norm=[0.1, 0.1, 0.4, 0.8])
    assert _alert_dedup_key(9, event).endswith(":3:track:42")


def test_minimum_object_area_filter() -> None:
    det = {"bbox_px": [10, 20, 30, 50]}
    assert _large_enough(det, 600) is True
    assert _large_enough(det, 601) is False


def test_detector_schedule_uses_store_local_time() -> None:
    # 07:00 UTC is 10:00 in Nairobi on this Thursday.
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    schedule = {"thu": ["09:00-18:00"]}
    assert _schedule_active(schedule, "Africa/Nairobi", now) is True
    assert _schedule_active({"thu": []}, "Africa/Nairobi", now) is False
    assert _schedule_active({"thu": ["11:00-18:00"]},
                            "Africa/Nairobi", now) is False
