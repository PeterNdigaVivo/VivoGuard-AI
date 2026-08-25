"""Recorder coverage and evidence retention."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import RecordingClip
from app.tasks.recorder import (
    _close_window, _current_window, _prune_expired_source_windows,
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


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


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
