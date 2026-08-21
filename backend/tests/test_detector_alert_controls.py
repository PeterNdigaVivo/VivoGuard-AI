from __future__ import annotations

from app.ai.detectors import staff_identity
from app.ai.detectors.base import DetectorContext
from app.ai.detectors.retail_p1 import IntrusionDetector
from app.ai.detectors.stateful import CrowdDetector
from app.ai.tracker import Track
from app.ai.tracker import partition_static_person_tracks


def _box(cx: float, cy: float = 0.5) -> list[float]:
    return [cx - 0.03, cy - 0.08, cx + 0.03, cy + 0.08]


def _person(track_id: int, history: list[list[float]], conf: float = 0.9):
    det = {
        "cls": "person", "conf": conf, "bbox_norm": history[-1],
        "track_id": track_id,
    }
    track = Track(track_id, "person", history[-1], 0.0, 1.0,
                  history=history)
    return track, det


def _crowd_context(timestamp: float, tracks: list[tuple], *,
                   zones: list[dict] | None = None,
                   dwell: float = 30, rearm: float = 60) -> DetectorContext:
    return DetectorContext(
        camera_id=17, timestamp=timestamp,
        raw_detections=[det for _track, det in tracks], tracks=tracks,
        zones=zones or [],
        config={"crowd": {
            "enabled": True, "confidence_threshold": 0.5,
            "crowd_threshold": 3, "dwell_time_seconds": dwell,
            "extra": {"incident_rearm_seconds": rearm},
        }},
    )


def test_crowd_requires_sustained_stationary_tracks() -> None:
    detector = CrowdDetector()
    stationary = [
        _person(i, [_box(0.2 * i)] * 8) for i in (1, 2, 3)
    ]
    assert detector.evaluate(_crowd_context(100, stationary)) == []
    events = detector.evaluate(_crowd_context(130, stationary))
    assert len(events) == 1
    assert events[0].extra["rule"] == "sustained_congregation"
    assert events[0].extra["track_ids"] == [1, 2, 3]


def test_moving_passersby_never_become_a_crowd() -> None:
    detector = CrowdDetector()
    moving = [
        _person(i, [_box(0.1 * i + step * 0.02) for step in range(8)])
        for i in (1, 2, 3)
    ]
    assert detector.evaluate(_crowd_context(100, moving, dwell=0)) == []
    assert detector.evaluate(_crowd_context(200, moving, dwell=0)) == []


def test_crowd_defaults_match_reviewed_retail_rule() -> None:
    assert CrowdDetector.DEFAULT_THRESHOLD == 6
    assert CrowdDetector.DEFAULT_DWELL_SECONDS == 300


def test_crowd_is_latched_until_continuous_clear_rearm() -> None:
    detector = CrowdDetector()
    stationary = [_person(i, [_box(0.2 * i)] * 8) for i in (1, 2, 3)]
    assert len(detector.evaluate(_crowd_context(100, stationary, dwell=0))) == 1
    assert detector.evaluate(_crowd_context(101, stationary, dwell=0)) == []
    assert detector.evaluate(_crowd_context(110, [], dwell=0, rearm=60)) == []
    assert detector.evaluate(_crowd_context(169, [], dwell=0, rearm=60)) == []
    assert detector.evaluate(_crowd_context(170, [], dwell=0, rearm=60)) == []
    assert len(detector.evaluate(_crowd_context(171, stationary, dwell=0))) == 1


def test_staff_area_rule_suppresses_generic_crowd_label() -> None:
    detector = CrowdDetector()
    stationary = [_person(i, [_box(0.2 * i)] * 8) for i in (1, 2, 3)]
    staff_zone = [{
        "id": 8, "suppressed": False,
        "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]],
        "detection_types_json": ["staff_zone"],
    }]
    assert detector.evaluate(
        _crowd_context(100, stationary, zones=staff_zone, dwell=0)
    ) == []


def test_intrusion_dedupes_per_track_and_rearms_after_absence(
    monkeypatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.ai.detectors.retail_p1.time.time", lambda: now[0])
    monkeypatch.setattr(staff_identity, "match_track",
                        lambda _ctx, det: det["track_id"])
    monkeypatch.setattr(staff_identity, "classify", lambda *_args: {
        "level": "unknown",
    })
    monkeypatch.setattr(IntrusionDetector, "_persistent_incident_allowed",
                        lambda *_args, **_kwargs: True)
    detector = IntrusionDetector()
    person = _person(41, [_box(0.5)] * 8)[1]
    cfg = {"intrusion": {
        "enabled": True, "confidence_threshold": 0.5,
        "extra": {"always_armed": True, "incident_rearm_seconds": 300},
    }}

    def context(detections):
        return DetectorContext(
            camera_id=17, timestamp=now[0], raw_detections=detections,
            tracks=[], zones=[], config=cfg, store_id=None,
        )

    assert len(detector.evaluate(context([person]))) == 1
    now[0] += 60
    assert detector.evaluate(context([person])) == []
    now[0] += 299
    assert detector.evaluate(context([])) == []
    now[0] += 1
    assert detector.evaluate(context([])) == []
    assert len(detector.evaluate(context([person]))) == 1


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    def set(self, key, value, *, ex, nx):  # noqa: ARG002
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def expire(self, key, ttl):  # noqa: ARG002
        return key in self.values


def test_intrusion_static_warmup_and_task_restart_share_persistent_dedupe(
    monkeypatch,
) -> None:
    """The fixed mannequin is actionable for nine warm-up frames, but must
    still create at most one incident across fresh detector/task instances."""
    fake_redis = _FakeRedis()
    monkeypatch.setattr("redis.from_url", lambda _url: fake_redis)
    monkeypatch.setattr(staff_identity, "match_track",
                        lambda _ctx, det: det["track_id"])
    monkeypatch.setattr(staff_identity, "classify", lambda *_args: {
        "level": "unknown",
    })
    cfg = {"intrusion": {
        "enabled": True, "confidence_threshold": 0.5,
        "extra": {"always_armed": True, "incident_rearm_seconds": 300},
    }}
    bbox = _box(0.5)

    def run_new_task() -> list:
        detector = IntrusionDetector()
        events = []
        for frame_number in range(1, 11):
            det = {"cls": "person", "conf": 0.9, "bbox_norm": bbox,
                   "track_id": 1}
            track = Track(1, "person", bbox, 0.0, float(frame_number),
                          history=[bbox] * frame_number)
            raw, tracks, _static = partition_static_person_tracks(
                [det], [(track, det)], (640, 360), window=10,
            )
            ctx = DetectorContext(
                camera_id=17, timestamp=float(frame_number),
                raw_detections=raw, tracks=tracks, zones=[], config=cfg,
                store_id=None,
            )
            events.extend(detector.evaluate(ctx))
        return events

    first_task = run_new_task()
    second_task = run_new_task()
    assert len(first_task) == 1
    assert first_task[0].track_id == 1
    assert second_task == []
