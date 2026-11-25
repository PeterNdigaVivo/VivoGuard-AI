"""PersonDetector staff-exclusion gate.

Daytime/after-hours person-alert policy lives in the inference
worker's _person_alert_warranted() (see test_person_alert_fail_open);
the DETECTOR's only extra gate is staff exclusion, covered here.
"""
from app.ai.detectors.base import DetectorContext
from app.ai.detectors.simple import PersonDetector

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


def _ctx(zones, extra=None):
    cfg = {"enabled": True, "confidence_threshold": 0.6}
    if extra:
        cfg["extra"] = extra
    return DetectorContext(
        camera_id=1, timestamp=1.0, raw_detections=[_person_det()],
        tracks=[], zones=zones, config={"person": cfg},
    )


def test_detector_emits_zone_tagged_event() -> None:
    event = PersonDetector().evaluate(_ctx([PERSON_ZONE]))[0]
    assert event.zone_id == 7
    assert event.confidence == 0.91


def test_zoneless_person_still_reaches_the_worker() -> None:
    """No detector-level zone gate: the worker's fail-open
    _person_alert_warranted() decides, so a 2 a.m. break-in on a
    zoneless camera is never dropped here."""
    assert len(PersonDetector().evaluate(_ctx([]))) == 1


def test_person_in_pure_staff_zone_is_silent() -> None:
    assert PersonDetector().evaluate(_ctx([PERSON_ZONE, STAFF_ZONE])) == []


def test_include_staff_escape_hatch() -> None:
    events = PersonDetector().evaluate(
        _ctx([PERSON_ZONE, STAFF_ZONE], extra={"include_staff": True}))
    assert len(events) == 1


def test_unreadable_pixels_still_alert() -> None:
    """No frame -> uniform classifier says 'uncertain' -> the alert
    still fires. Only confident staff verdicts are excluded."""
    assert len(PersonDetector().evaluate(_ctx([PERSON_ZONE]))) == 1
