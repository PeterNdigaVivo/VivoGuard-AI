from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Camera, DetectionEvent, RecordingClip, Store, Zone
from app.tasks.recorder import (
    _clip_source_matches_event, _entrance_clip_for, _recording_covers_event,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_closed_recording_must_cover_event_timestamp(tmp_path):
    event_ts = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
    path = tmp_path / "old.mp4"
    path.write_bytes(b"old recording")
    clip = RecordingClip(
        camera_id=1, window_id="old", file_path=str(path),
        started_at=event_ts - timedelta(minutes=30),
        ended_at=event_ts - timedelta(seconds=1), status="completed",
    )

    assert _recording_covers_event(clip, event_ts) is False


def test_entrance_selection_skips_newer_non_covering_recording(tmp_path):
    db = _session()
    store = Store(name="Evidence Store", country="Kenya")
    db.add(store); db.flush()
    anchor = Camera(
        name="Anchor", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.1", store_id=store.id,
    )
    entrance = Camera(
        name="Entrance", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.2", store_id=store.id,
    )
    db.add_all([anchor, entrance]); db.flush()
    db.add(Zone(
        camera_id=entrance.id, name="Door", shape="polygon",
        polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
        detection_types_json=["entry_exit"],
    ))
    event_ts = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
    valid_path = tmp_path / "valid.mp4"
    expired_path = tmp_path / "expired.mp4"
    valid_path.write_bytes(b"valid")
    expired_path.write_bytes(b"expired")
    valid = RecordingClip(
        camera_id=entrance.id, store_id=store.id, window_id="valid",
        file_path=str(valid_path), started_at=event_ts - timedelta(hours=1),
        ended_at=event_ts + timedelta(minutes=1), status="completed",
    )
    expired = RecordingClip(
        camera_id=entrance.id, store_id=store.id, window_id="expired",
        file_path=str(expired_path), started_at=event_ts - timedelta(minutes=10),
        ended_at=event_ts - timedelta(minutes=5), status="completed",
    )
    db.add_all([valid, expired]); db.flush()
    event = DetectionEvent(
        camera_id=anchor.id, detection_type="shop_open_close", confidence=1,
        bbox_json=[0, 0, 1, 1], timestamp=event_ts,
        extra={"store_id": store.id},
    )

    selected = _entrance_clip_for(db, event, event_ts)

    assert selected.id == valid.id


def test_cross_camera_evidence_requires_same_store_entrance(tmp_path):
    db = _session()
    store = Store(name="Bound Store", country="Kenya")
    other_store = Store(name="Other Store", country="Kenya")
    db.add_all([store, other_store]); db.flush()
    anchor = Camera(
        name="Anchor", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.1", store_id=store.id,
    )
    entrance = Camera(
        name="Entrance", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.2", store_id=store.id,
    )
    wrong_store = Camera(
        name="Wrong entrance", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.3", store_id=other_store.id,
    )
    db.add_all([anchor, entrance, wrong_store]); db.flush()
    db.add_all([
        Zone(camera_id=entrance.id, name="Door", shape="polygon",
             polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
             detection_types_json=["entry_exit"]),
        Zone(camera_id=wrong_store.id, name="Other door", shape="polygon",
             polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
             detection_types_json=["entry_exit"]),
    ])
    db.flush()
    event = DetectionEvent(
        camera_id=anchor.id, detection_type="shop_open_close", confidence=1,
        bbox_json=[0, 0, 1, 1], extra={"store_id": store.id},
    )
    valid = RecordingClip(
        camera_id=entrance.id, store_id=store.id, window_id="valid",
        file_path=str(tmp_path / "valid.mp4"),
    )
    invalid = RecordingClip(
        camera_id=wrong_store.id, store_id=other_store.id, window_id="invalid",
        file_path=str(tmp_path / "invalid.mp4"),
    )

    assert _clip_source_matches_event(db, event, valid) is True
    assert _clip_source_matches_event(db, event, invalid) is False
    event.detection_type = "intrusion"
    assert _clip_source_matches_event(db, event, valid) is False
