"""Regression tests for controls that prevent frame noise becoming alerts."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.ai.detectors.base import DetectorContext
from app.ai.detectors.simple import PersonDetector
from app.ai.inference_worker import (_alert_dedup_key, _large_enough,
                                     _schedule_active)


PERSON_ZONE = {"id": 7, "detection_types_json": ["person"],
               "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]]}
STAFF_ZONE = {"id": 8, "detection_types_json": ["staff_zone"],
              "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]]}


def _person_det(**over) -> dict:
    det = {"cls": "person", "conf": 0.91, "rolling_conf": 0.84,
           "bbox_norm": [0.1, 0.1, 0.4, 0.8],
           "bbox_px": [10, 10, 40, 80], "track_id": 42}
    det.update(over)
    return det


def test_stateless_detector_preserves_tracker_identity() -> None:
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=[PERSON_ZONE],
        config={"person": {"enabled": True, "confidence_threshold": 0.6}},
    )
    event = PersonDetector().evaluate(ctx)[0]
    assert event.track_id == 42
    assert event.confidence == 0.84


def test_person_without_zone_is_silent() -> None:
    """Nov-2026 false-alarm fix: a camera with no person-tagged zone
    raises no person alerts (93% of the urgent backlog was zoneless
    daytime person noise)."""
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=[],
        config={"person": {"enabled": True, "confidence_threshold": 0.6}},
    )
    assert PersonDetector().evaluate(ctx) == []


def test_person_zoneless_escape_hatch() -> None:
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=[],
        config={"person": {"enabled": True, "confidence_threshold": 0.6,
                           "extra": {"allow_zoneless": True}}},
    )
    assert len(PersonDetector().evaluate(ctx)) == 1


def test_person_in_pure_staff_zone_is_silent() -> None:
    """A person standing in a staff_zone is staff turf — not a person
    alert, whatever the uniform classifier can or can't read."""
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=[PERSON_ZONE, STAFF_ZONE],
        config={"person": {"enabled": True, "confidence_threshold": 0.6}},
    )
    assert PersonDetector().evaluate(ctx) == []


def test_person_unreadable_pixels_still_alert() -> None:
    """No frame -> staff classifier returns 'uncertain' -> the alert
    still fires. Only confident staff verdicts are excluded."""
    ctx = DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=[PERSON_ZONE],
        config={"person": {"enabled": True, "confidence_threshold": 0.6}},
    )
    assert len(PersonDetector().evaluate(ctx)) == 1


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
