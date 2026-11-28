from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Alert, Camera, DetectionEvent, RecordingClip, Store, Zone
from app.tasks import recorder
from app.tasks.recorder import (
    _clip_source_matches_event, _entrance_clip_for,
    _pending_alert_clip_rows, _pending_open_close_clip_rows, _prunable_alert_clip_rows,
    _recording_covers_event,
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


@pytest.mark.parametrize("status", ["recording", "completed"])
def test_source_coverage_requires_nonempty_media(tmp_path, status):
    event_ts = datetime.now(timezone.utc)
    path = tmp_path / "source.mp4"
    clip = RecordingClip(
        camera_id=1, window_id="current", file_path=str(path),
        started_at=event_ts - timedelta(minutes=1), status=status,
        ended_at=event_ts + timedelta(seconds=1) if status == "completed" else None,
    )

    assert not _recording_covers_event(clip, event_ts)
    path.touch()
    assert not _recording_covers_event(clip, event_ts)
    path.write_bytes(b"recorded source fixture")
    assert _recording_covers_event(clip, event_ts)


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


def test_pending_clip_batch_excludes_completed_work_before_limit(tmp_path):
    db = _session()
    camera = Camera(
        name="Busy Camera", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.5",
    )
    db.add(camera); db.flush()
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"recording")
    db.add(RecordingClip(
        camera_id=camera.id, window_id="busy", file_path=str(source),
        started_at=now - timedelta(hours=1),
        ended_at=now + timedelta(hours=1), status="completed",
    ))
    completed_event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=0.9,
        bbox_json=[0, 0, 1, 1], timestamp=now,
        extra={"alert_clip_path": "/clips/already-done.mp4"},
    )
    missing_event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=0.9,
        bbox_json=[0, 0, 1, 1], timestamp=now - timedelta(minutes=10),
        extra={},
    )
    db.add_all([completed_event, missing_event]); db.flush()
    completed_alert = Alert(
        event_id=completed_event.id, created_at=now,
    )
    missing_alert = Alert(
        event_id=missing_event.id, created_at=now - timedelta(minutes=10),
    )
    db.add_all([completed_alert, missing_alert]); db.commit()

    rows = _pending_alert_clip_rows(
        db, now - timedelta(hours=2), limit=1,
    )

    assert [alert.id for alert, _event, _clip in rows] == [missing_alert.id]


def test_open_close_backfill_excludes_completed_clips_before_limit():
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    with _session() as db:
        camera = Camera(
            name="Unrecorded Anchor", brand="generic", connection_type="lan_rtsp",
            host="127.0.0.1",
        )
        db.add(camera); db.flush()

        cases = [
            (now, "shop_open_close", {"alert_clip_path": "/clips/done.mp4"}),
            (now, "intrusion", {}),
            (now - timedelta(minutes=5), "shop_open_close", {"alert_clip_path": ""}),
            (now - timedelta(minutes=10), "shop_open_close", {}),
            (now - timedelta(minutes=15), "shop_open_close", None),
            (now - timedelta(days=2), "shop_open_close", {}),
        ]
        alerts = []
        for timestamp, detection_type, extra in cases:
            event = DetectionEvent(
                camera_id=camera.id, detection_type=detection_type, confidence=0.9,
                bbox_json=[0, 0, 1, 1], timestamp=timestamp, extra=extra,
            )
            db.add(event); db.flush()
            alert = Alert(event_id=event.id, created_at=timestamp)
            db.add(alert)
            alerts.append(alert)
        db.flush()

        rows = _pending_open_close_clip_rows(db, now - timedelta(days=1), limit=3)

        assert [alert.id for alert, _event in rows] == [alert.id for alert in alerts[2:5]]


@pytest.mark.parametrize(
    ("successful_source", "expected_attempts"),
    [(3, [3]), (2, [3, 2]), (None, [3, 2])],
)
def test_clip_extractor_retries_one_distinct_overlapping_source(
    monkeypatch, tmp_path, successful_source, expected_attempts,
):
    event_ts = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    alert = SimpleNamespace(id=1)
    event = SimpleNamespace(timestamp=event_ts, detection_type="intrusion", extra={})
    outside, newest, fallback, third = [
        SimpleNamespace(
            id=source_id,
            file_path=str(tmp_path / f"{source_id}.mp4"),
            started_at=event_ts - timedelta(minutes=10),
            ended_at=event_ts + timedelta(seconds=end_offset),
        )
        for source_id, end_offset in [(4, -1), (3, 30), (2, 30), (1, 30)]
    ]
    for source in (outside, newest, fallback, third):
        Path(source.file_path).write_bytes(b"recorded source fixture")
    rows = [(alert, event, source) for source in (outside, newest, newest, fallback, third)]
    attempted = []

    def extract(_db, _alert, _event, clip, _timestamp):
        attempted.append(clip.id)
        return clip.id == successful_source

    monkeypatch.setattr(recorder.settings, "recording_enabled", True)
    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(recorder, "_alert_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder, "_pending_alert_clip_rows", lambda *_args: rows)
    monkeypatch.setattr(recorder, "_pending_open_close_clip_rows", lambda *_args: [])
    monkeypatch.setattr(recorder, "_extract_one", extract)

    recorder.extract_pending_clips.run()

    assert attempted == expected_attempts


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("entrance_succeeds", [False, True])
def test_open_clip_falls_back_to_event_recording_after_entrance_failure(
    monkeypatch, tmp_path, backfill, entrance_succeeds,
):
    event_ts = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    alert = SimpleNamespace(id=1)
    event = SimpleNamespace(
        camera_id=1, timestamp=event_ts, detection_type="shop_open_close", extra={},
    )
    entrance, own, unused = [
        SimpleNamespace(
            id=source_id, file_path=str(tmp_path / f"{source_id}.mp4"),
            started_at=event_ts - timedelta(minutes=10),
            ended_at=event_ts + timedelta(minutes=1),
        )
        for source_id in (2, 1, 3)
    ]
    for source in (entrance, own, unused):
        Path(source.file_path).write_bytes(b"synthetic source")
    db = Mock()
    query = db.query.return_value.filter.return_value.order_by.return_value.limit.return_value
    query.all.return_value = [own, unused]
    attempted = []

    def extract(_db, _alert, _event, source, _timestamp):
        attempted.append(source.id)
        return source.id == entrance.id and entrance_succeeds

    rows = [] if backfill else [(alert, event, own), (alert, event, unused)]
    pending = [(alert, event)] if backfill else []
    monkeypatch.setattr(recorder.settings, "recording_enabled", True)
    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(recorder, "_alert_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder, "_pending_alert_clip_rows", lambda *_args: rows)
    monkeypatch.setattr(recorder, "_pending_open_close_clip_rows", lambda *_args: pending)
    monkeypatch.setattr(recorder, "_entrance_clip_for", lambda *_args: entrance)
    monkeypatch.setattr(recorder, "_extract_one", extract)

    recorder.extract_pending_clips.run()

    assert attempted == ([entrance.id] if entrance_succeeds else [entrance.id, own.id])


@pytest.mark.parametrize(
    "failure", [None, "ffmpeg", "timeout", "checksum", "audio_only", "zero_frames"],
)
def test_extraction_publishes_only_a_hashed_video_encode(monkeypatch, tmp_path, failure):
    event_ts = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    alert = SimpleNamespace(id=1)
    published = tmp_path / "1.mp4"
    previous_bytes = b"previous clip"
    published.write_bytes(previous_bytes)
    original_extra = {"alert_clip_path": str(published),
                      "alert_clip_sha256": sha256(previous_bytes).hexdigest()}
    event = SimpleNamespace(camera_id=2, detection_type="intrusion", extra=original_extra)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"recorded source fixture")
    clip = RecordingClip(
        id=3, camera_id=2, window_id="source", file_path=str(source),
        started_at=event_ts - timedelta(minutes=10),
        ended_at=event_ts + timedelta(minutes=1), status="completed",
    )

    encoded = b"new encoded clip"

    def encode(cmd, **kwargs):
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert kwargs["stdout"] == recorder.subprocess.PIPE
        staged = Path(cmd[-1])
        assert staged.parent.parent == tmp_path
        staged.write_bytes(encoded)
        assert published.read_bytes() == previous_bytes
        if failure == "timeout":
            raise recorder.subprocess.TimeoutExpired(cmd, 180)
        progress = {
            "audio_only": b"total_size=200\nprogress=end\n",
            "zero_frames": b"frame=0\nprogress=continue\nframe=  0\nprogress=end\n",
        }.get(failure, b"frame=0\nprogress=continue\nframe=  5\nprogress=end\n")
        return SimpleNamespace(returncode=int(failure == "ffmpeg"), stdout=progress)

    def fail_checksum(_path):
        raise OSError("unreadable staged clip")

    monkeypatch.setattr(recorder, "_alert_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder.settings, "incident_foundations_enabled", False)
    monkeypatch.setattr(recorder.subprocess, "run", encode)
    if failure == "checksum":
        monkeypatch.setattr(recorder, "_sha256_file", fail_checksum)

    db = Mock()
    assert recorder._extract_one(db, alert, event, clip, event_ts) is (failure is None)
    assert set(tmp_path.iterdir()) == {published, source}
    if failure is None:
        assert published.read_bytes() == encoded
        assert event.extra["alert_clip_sha256"] == sha256(encoded).hexdigest()
        assert event.extra["alert_clip_source_recording_id"] == clip.id
        db.commit.assert_called_once_with()
    else:
        assert published.read_bytes() == previous_bytes
        assert event.extra == original_extra
        db.commit.assert_not_called()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
@pytest.mark.parametrize("with_video", [True, False], ids=["video", "audio_only"])
def test_clip_publish_checks_encoded_video_frames(monkeypatch, tmp_path, with_video):
    source = tmp_path / "source.mp4"
    media_args = (
        ["-i", "color=size=64x64:rate=5", "-c:v", "mpeg4"]
        if with_video else ["-i", "sine=frequency=400", "-c:a", "aac"]
    )
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         *media_args, "-t", "1", str(source)],
        check=True, capture_output=True, timeout=10,
    )
    event_ts = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    event = SimpleNamespace(camera_id=2, detection_type="intrusion", extra={})
    clip = RecordingClip(
        id=3, camera_id=2, window_id="source", file_path=str(source),
        started_at=event_ts, ended_at=event_ts + timedelta(seconds=1), status="completed",
    )
    db = Mock()
    monkeypatch.setattr(recorder, "_alert_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder.settings, "incident_foundations_enabled", False)

    assert recorder._extract_one(db, SimpleNamespace(id=1), event, clip, event_ts) is with_video

    output = tmp_path / "1.mp4"
    assert output.is_file() is with_video
    assert db.commit.call_count == int(with_video)
    if with_video:
        assert event.extra["alert_clip_sha256"] == sha256(output.read_bytes()).hexdigest()
    else:
        assert event.extra == {}


def test_alert_clip_pruning_protects_unresolved_and_escalated_evidence():
    db = _session()
    store = Store(name="Retention Store", country="Kenya")
    db.add(store); db.flush()
    camera = Camera(
        name="Retention Camera", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.4", store_id=store.id,
    )
    db.add(camera); db.flush()
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=48)
    cases = [
        ("dismissed", None, now - timedelta(hours=72), True),
        ("confirmed", now - timedelta(hours=60), now - timedelta(hours=72), True),
        ("resolved", now - timedelta(hours=60), now - timedelta(hours=72), True),
        ("new", None, now - timedelta(hours=72), False),
        ("escalated", None, now - timedelta(hours=72), False),
        ("confirmed", None, now - timedelta(hours=72), False),
        ("dismissed", None, now - timedelta(hours=12), False),
    ]
    expected: set[int] = set()
    for status, resolved_at, created_at, prunable in cases:
        event = DetectionEvent(
            camera_id=camera.id, detection_type="intrusion", confidence=0.9,
            bbox_json=[0, 0, 1, 1],
            extra={"alert_clip_path": f"/clips/{status}-{created_at.hour}.mp4"},
        )
        db.add(event); db.flush()
        alert = Alert(
            event_id=event.id, status=status, resolved_at=resolved_at,
            created_at=created_at,
        )
        db.add(alert); db.flush()
        if prunable:
            expected.add(alert.id)
    db.commit()

    rows = _prunable_alert_clip_rows(db, cutoff)

    assert {alert.id for alert, _event in rows} == expected


def test_alert_clip_retention_retries_failed_file_removal(monkeypatch, tmp_path, caplog):
    clip = tmp_path / "retention-fixture.mp4"
    clip.write_bytes(b"test recording")
    with _session() as db:
        camera = Camera(
            name="Retention Camera", brand="generic", connection_type="lan_rtsp",
            host="127.0.0.1",
        )
        db.add(camera); db.flush()
        old = datetime.now(timezone.utc) - timedelta(days=7)
        evidence = {"alert_clip_path": str(clip), "alert_clip_source_camera_id": camera.id,
                    "alert_clip_sha256": "a" * 64, "other": "retain"}
        event = DetectionEvent(
            camera_id=camera.id, detection_type="intrusion", confidence=0.9,
            bbox_json=[0, 0, 1, 1], timestamp=old, extra=evidence,
        )
        db.add(event); db.flush()
        db.add(Alert(event_id=event.id, status="resolved", created_at=old, resolved_at=old))
        db.commit()
        monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(db))
        monkeypatch.setattr(recorder.settings, "recording_alert_clip_retention_hours", 48)
        unlink = Path.unlink

        def denied(path, *, missing_ok=False):
            if path == clip:
                raise PermissionError("private storage diagnostic")
            return unlink(path, missing_ok=missing_ok)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", denied)
            recorder.prune_alert_clips.run()

        db.refresh(event)
        assert clip.exists()
        assert event.extra == evidence
        assert "removal failed for 1 retention candidate(s)" in caplog.text
        assert str(clip) not in caplog.text
        assert "private storage diagnostic" not in caplog.text

        recorder.prune_alert_clips.run()

        db.refresh(event)
        assert not clip.exists()
        assert event.extra == {"other": "retain"}
