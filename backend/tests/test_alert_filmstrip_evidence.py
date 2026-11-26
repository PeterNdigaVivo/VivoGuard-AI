from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.tasks import alert_snapshots


@pytest.mark.parametrize("has_recorded_frame", [True, False])
def test_pre_event_frame_requires_recorded_footage(monkeypatch, has_recorded_frame):
    when = datetime(2026, 8, 25, 9, tzinfo=timezone.utc)
    recorded = b"recorded pre-event JPEG"
    live = b"current JPEG"
    pre = Mock(side_effect=lambda _camera, timestamp: recorded if has_recorded_frame and timestamp < when else None)
    redis_frame = Mock(return_value=live)
    save = Mock(return_value="stored")
    append = Mock()
    schedule = Mock()
    monkeypatch.setattr(alert_snapshots, "settings", SimpleNamespace(business_hours_snapshot_enabled=True))
    monkeypatch.setattr(alert_snapshots, "_extract_recording_frame", pre)
    monkeypatch.setattr(alert_snapshots, "_redis_frame", redis_frame)
    monkeypatch.setattr(alert_snapshots, "_save_jpeg", save)
    monkeypatch.setattr(alert_snapshots, "_append_path", append)
    monkeypatch.setattr(alert_snapshots.capture_filmstrip_frame, "apply_async", schedule)

    alert_snapshots.schedule_alert_filmstrip.run(5, 6, 7, "intrusion", when.timestamp())

    pre_at = when - timedelta(seconds=10)
    assert [call.args for call in pre.call_args_list if call.args[1] < when] == [(6, pre_at)]
    redis_frame.assert_called_once_with(6)
    saved = [(call.args[0], call.kwargs["epoch_ts"]) for call in save.call_args_list]
    current = (live, int(when.timestamp()))
    if has_recorded_frame:
        assert saved == [(recorded, int(pre_at.timestamp())), current]
    else:
        assert saved == [current]
    assert append.call_count == len(saved)
    assert schedule.call_count == len(alert_snapshots.DEFAULT_POST_OFFSETS_S)


@pytest.mark.parametrize(("exit_code", "output", "expected"), [
    (0, b"JPEG", b"JPEG"),
    (0, b"", None),
    (1, b"partial", None),
])
def test_pre_event_extractor_only_accepts_successful_ffmpeg_output(
    monkeypatch, tmp_path, exit_code, output, expected,
):
    when = datetime(2026, 8, 25, 9, tzinfo=timezone.utc)
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"recorded fixture")
    clip = SimpleNamespace(started_at=when - timedelta(seconds=15), file_path=str(source))
    db = MagicMock()
    query = db.__enter__.return_value.query.return_value
    query.filter.return_value.order_by.return_value.first.return_value = clip
    encode = Mock(return_value=SimpleNamespace(returncode=exit_code, stdout=output))
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    monkeypatch.setattr(alert_snapshots.subprocess, "run", encode)

    assert alert_snapshots._extract_recording_frame(6, when) == expected

    args = encode.call_args.args[0]
    assert args[args.index("-ss") + 1] == "15"
    assert args[-1] == "pipe:1"
    assert encode.call_args.kwargs["stdout"] == alert_snapshots.subprocess.PIPE
