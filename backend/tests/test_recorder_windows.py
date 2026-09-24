"""Recorder coverage and evidence retention."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import RecordingClip
from app.tasks.recorder import (
    _close_window, _current_window, _prune_expired_source_windows,
    _recording_health_snapshot, _recording_path,
)


EAT = ZoneInfo("Africa/Nairobi")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 21, hour, minute, tzinfo=EAT)


def test_recorder_has_no_daily_coverage_gap() -> None:
    assert all(_current_window(_at(hour, 30)) is not None for hour in range(24))


def test_after_hours_windows_are_bounded_and_date_stamped() -> None:
    midnight = _current_window(_at(3, 22))
    evening = _current_window(_at(23, 59))
    assert midnight is not None and midnight[:2] == ("20260821_0000", 25200)
    assert evening is not None and evening[:2] == ("20260821_2000", 14400)


def test_restart_uses_continuation_path_without_truncating_source(tmp_path) -> None:
    original = tmp_path / "7.mp4"
    original.write_bytes(b"pre-restart evidence")

    continuation = _recording_path(tmp_path, 7)

    assert continuation != original
    assert continuation.name.startswith("7_")
    assert continuation.suffix == ".mp4"
    assert original.read_bytes() == b"pre-restart evidence"


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _RecorderHealthRedis:
    def __init__(self):
        self.values = {
            "vg:recording:pid:1": json.dumps({
                "pid": 101, "window_id": "current",
            }),
            "vg:recording:pid:2": json.dumps({
                "pid": 102, "window_id": "current",
            }),
            "vg:recording:pid:3": json.dumps({
                "pid": 103, "window_id": "previous",
            }),
            "vg:recording:pid:99": json.dumps({
                "pid": 199, "window_id": "current",
            }),
        }

    def scan_iter(self, *, match: str, count: int):
        assert match == "vg:recording:pid:*"
        assert count == 200
        return iter(self.values)

    def get(self, key: str):
        return self.values.get(key)


def test_recording_health_counts_only_expected_live_cameras(monkeypatch) -> None:
    expected_cameras = [
        SimpleNamespace(id=camera_id) for camera_id in range(1, 4)
    ]
    monkeypatch.setattr(
        "app.tasks.recorder._key_cameras", lambda _db: expected_cameras,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._pid_is_ffmpeg", lambda pid: pid in {101, 199},
    )

    result = _recording_health_snapshot(
        object(), _RecorderHealthRedis(), "current", now=1_000.0,
    )

    assert result == {
        "last_run_ts": 1_000.0,
        "window_active": True,
        "cameras_expected": 3,
        "cameras_recording": 1,
        "cameras_missing": 2,
        "status": "degraded",
    }
    assert "camera_ids" not in result


def test_close_window_retains_source_for_delayed_extraction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    db = _session()
    source = tmp_path / "clips" / "20260824_1400" / "1" / "7.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"recoverable incident source")
    clip = RecordingClip(
        camera_id=7, window_id="20260824_1400", file_path=str(source),
        status="recording",
    )
    db.add(clip)
    db.commit()
    ended = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)

    assert _close_window(db, clip.window_id, ended_at=ended) == 1

    db.refresh(clip)
    assert clip.status == "completed"
    assert clip.file_path == str(source)
    assert source.exists()


def test_prune_source_window_only_after_retention(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    db = _session()
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    recent = tmp_path / "clips" / "recent" / "1" / "1.mp4"
    expired = tmp_path / "clips" / "expired" / "1" / "2.mp4"
    recent.parent.mkdir(parents=True)
    expired.parent.mkdir(parents=True)
    recent.write_bytes(b"recent")
    expired.write_bytes(b"expired")
    db.add_all([
        RecordingClip(
            camera_id=1, window_id="recent", file_path=str(recent),
            status="completed", ended_at=now - timedelta(hours=11),
        ),
        RecordingClip(
            camera_id=2, window_id="expired", file_path=str(expired),
            status="completed", ended_at=now - timedelta(hours=13),
        ),
    ])
    db.commit()

    assert _prune_expired_source_windows(
        db, now=now, retention_hours=12,
    ) == 1

    rows = {row.window_id: row for row in db.query(RecordingClip).all()}
    assert rows["recent"].status == "completed"
    assert rows["recent"].file_path == str(recent)
    assert recent.exists()
    assert rows["expired"].status == "deleted"
    assert rows["expired"].file_path is None
    assert not expired.exists()


def test_prune_does_not_delete_window_shared_with_active_recorder(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    db = _session()
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    source = tmp_path / "clips" / "shared" / "1" / "1.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"active")
    db.add_all([
        RecordingClip(
            camera_id=1, window_id="shared", file_path=str(source),
            status="completed", ended_at=now - timedelta(hours=13),
        ),
        RecordingClip(
            camera_id=2, window_id="shared", file_path=str(source),
            status="recording",
        ),
    ])
    db.commit()

    assert _prune_expired_source_windows(
        db, now=now, retention_hours=12,
    ) == 0
    assert source.exists()
