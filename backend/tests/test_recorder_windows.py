"""Recorder coverage and evidence retention."""
import io
import json
import os
import signal
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import Camera, DetectionEvent, RecordingClip, Zone
from app.services.incident_foundations import clip_eligibility
from app.tasks import recorder
from app.tasks.recorder import (
    _classify_ffmpeg_exit,
    _close_stale_camera_recordings, _close_window, _current_window,
    _owned_recorder_pids, _pid_is_ffmpeg, _prune_expired_source_windows,
    _recording_health_snapshot,
    _recording_output_has_data, _recording_path, _repair_missing_recordings,
    _start_camera, _stop_all, _stop_tracked_camera_recorder,
    _tracking_ttl, _watch_recorder_exit,
)


EAT = ZoneInfo("Africa/Nairobi")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 21, hour, minute, tzinfo=EAT)


def test_recorder_has_no_daily_coverage_gap() -> None:
    assert all(_current_window(_at(hour, 30)) is not None for hour in range(24))


def test_tracking_ttl_outlives_longest_recording_window() -> None:
    seven_hours = 7 * 60 * 60

    assert _tracking_ttl(seven_hours) == seven_hours + 10 * 60
    assert _tracking_ttl(seven_hours) > 6 * 60 * 60


@pytest.mark.parametrize(("saved_transport", "expected_flag"), [
    ("tcp", "tcp"), ("http", "http"), ("udp", "udp"),
    (None, "tcp"), ("legacy-invalid", "tcp"),
])
def test_started_camera_uses_saved_transport_and_tracks_full_runtime(
    monkeypatch, tmp_path, saved_transport, expected_flag,
) -> None:
    writes = []

    class _Redis:
        def set(self, key, value, **kwargs):
            writes.append((key, json.loads(value), kwargs))
            return True

        def delete(self, _key):
            pass

    class _Db:
        def add(self, _row):
            pass

    monkeypatch.setattr(
        "app.tasks.recorder._substream_url", lambda _cam: "rtsp://redacted",
    )
    monkeypatch.setattr("app.tasks.recorder._clips_root", lambda: tmp_path)
    spawn = Mock(return_value=SimpleNamespace(pid=321))
    monkeypatch.setattr("app.tasks.recorder.subprocess.Popen", spawn)
    monkeypatch.setattr("app.tasks.recorder._start_exit_watcher", lambda *_args: None)

    seconds = 7 * 60 * 60
    camera = SimpleNamespace(id=7, store_id=3, rtsp_transport=saved_transport)
    assert _start_camera(_Db(), _Redis(), camera, "window", seconds)
    args = spawn.call_args.args[0]
    assert args[args.index("-rtsp_transport") + 1] == expected_flag
    assert writes == [(
        "vg:recording:pid:7",
        {"pid": 321, "window_id": "window", "path": str(tmp_path / "window" / "3" / "7.mp4")},
        {"ex": seconds + 10 * 60},
    )]


@pytest.mark.parametrize("failure", [False, RuntimeError("private tracking error")])
def test_unregistered_recorder_is_stopped_and_reaped(monkeypatch, tmp_path, caplog, failure):
    redis = Mock()
    if isinstance(failure, Exception):
        redis.set.side_effect = failure
    else:
        redis.set.return_value = failure
    db = Mock()
    proc = Mock(pid=321)
    watcher = Mock()
    monkeypatch.setattr(recorder, "_substream_url", lambda _camera: "rtsp://camera.example/stream")
    monkeypatch.setattr(recorder, "_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(recorder, "_start_exit_watcher", watcher)

    assert not _start_camera(db, redis, SimpleNamespace(id=7, store_id=3), "window", 600)

    proc.kill.assert_called_once_with()
    watcher.assert_called_once_with(proc, 7, None)
    db.add.assert_not_called()
    redis.delete.assert_not_called()
    assert "could not register newly spawned source" in caplog.text
    assert "private tracking error" not in caplog.text


def test_window_start_continues_when_one_store_output_path_is_blocked(
    monkeypatch, tmp_path, caplog,
) -> None:
    blocked_dir = tmp_path / "window" / "3"
    blocked_dir.parent.mkdir()
    blocked_dir.write_text("existing data", encoding="utf-8")
    cameras = [SimpleNamespace(id=7, store_id=3), SimpleNamespace(id=8, store_id=4)]
    redis = Mock()
    db = Mock()
    proc = Mock(pid=321)
    spawn = Mock(return_value=proc)
    watcher = Mock()
    monkeypatch.setattr(recorder, "_clips_root", lambda: tmp_path)
    monkeypatch.setattr(recorder, "_key_cameras", lambda _db: cameras)
    monkeypatch.setattr(recorder, "_substream_url", lambda _camera: "rtsp://redacted")
    monkeypatch.setattr(recorder.subprocess, "Popen", spawn)
    monkeypatch.setattr(recorder, "_start_exit_watcher", watcher)

    assert recorder._start_window(db, redis, "window", 600) == 1

    assert spawn.call_count == 1
    assert spawn.call_args.args[0][-1] == str(tmp_path / "window" / "4" / "8.mp4")
    watcher.assert_called_once_with(proc, 8, redis)
    assert db.add.call_args.args[0].camera_id == 8
    db.commit.assert_called_once_with()
    assert redis.set.call_args_list[0].args == (
        "vg:recording:failure:7", "output_path_unavailable",
    )
    assert blocked_dir.read_text(encoding="utf-8") == "existing data"
    assert str(tmp_path) not in caplog.text


def test_ffmpeg_exit_reason_never_contains_rtsp_credentials() -> None:
    stderr = (
        b"[rtsp] method DESCRIBE failed: 401 Unauthorized\n"
        b"rtsp://operator:super-secret@camera.internal/Streaming/Channels/102"
    )

    assert _classify_ffmpeg_exit(stderr) == "authentication_failed"


def test_ffmpeg_exit_reason_classifies_connection_limit() -> None:
    assert _classify_ffmpeg_exit(b"RTSP/1.0 453 Not Enough Bandwidth") == (
        "connection_limit"
    )


@pytest.mark.parametrize(("stderr", "returncode", "reason"), [
    (
        b"[mp4] Could not find tag for codec pcm_mulaw in stream #1, "
        b"codec not currently supported in container\n"
        b"Could not write header: Invalid argument",
        234,
        "unsupported_output_codec",
    ),
    (b"", 234, "ffmpeg_einval"),
    (b"Error opening output files: Invalid argument", 1, "ffmpeg_einval"),
    (b"RTSP/1.0 401 Unauthorized", 234, "authentication_failed"),
    (b"Unexpected failure", 1, "unclassified"),
])
def test_ffmpeg_exit_preserves_specific_reason_before_errno_fallback(
    stderr, returncode, reason,
):
    assert _classify_ffmpeg_exit(stderr, returncode=returncode) == reason


def test_recorder_watcher_reports_einval_without_persisting_stderr(caplog):
    redis = Mock()
    proc = SimpleNamespace(
        returncode=234,
        communicate=lambda: (b"", b"rtsp://operator:example-secret@camera.invalid/"),
    )

    _watch_recorder_exit(proc, camera_id=7, r=redis)

    redis.set.assert_called_once_with(
        "vg:recording:failure:7", "ffmpeg_einval", ex=24 * 3600,
    )
    assert "reason=ffmpeg_einval" in caplog.text
    assert "example-secret" not in caplog.text
    assert "camera.invalid" not in caplog.text


def test_ffmpeg_exit_log_never_exposes_rtsp_credentials(caplog) -> None:
    proc = SimpleNamespace(
        returncode=1,
        communicate=lambda: (
            b"",
            b"rtsp://operator:super-secret@camera.internal failed: "
            b"401 Unauthorized",
        ),
    )

    _watch_recorder_exit(proc, camera_id=7)

    assert "reason=authentication_failed" in caplog.text
    assert "super-secret" not in caplog.text
    assert "camera.internal" not in caplog.text


def test_ffmpeg_exit_persists_only_safe_failure_category() -> None:
    writes = []

    class _Redis:
        def set(self, key, value, **kwargs):
            writes.append((key, value, kwargs))

    proc = SimpleNamespace(
        returncode=1,
        communicate=lambda: (
            b"",
            b"rtsp://operator:super-secret@camera.internal failed: "
            b"401 Unauthorized",
        ),
    )

    _watch_recorder_exit(proc, camera_id=7, r=_Redis())

    assert writes == [(
        "vg:recording:failure:7",
        "authentication_failed",
        {"ex": 24 * 3600},
    )]


def test_owned_recorder_pid_discovery_excludes_alert_extractors(
    monkeypatch, tmp_path,
) -> None:
    proc = tmp_path / "proc"
    source = proc / "101"
    extract = proc / "202"
    unrelated = proc / "303"
    for directory in (source, extract, unrelated):
        directory.mkdir(parents=True)
    (source / "cmdline").write_bytes(
        b"ffmpeg\0-i\0rtsp://redacted\0-c\0copy\0-movflags\0"
        b"+frag_keyframe+empty_moov\0/source.mp4\0"
    )
    (extract / "cmdline").write_bytes(
        b"ffmpeg\0-i\0/source.mp4\0-c:v\0libx264\0-movflags\0"
        b"+faststart\0/alert.mp4\0"
    )
    (unrelated / "cmdline").write_bytes(b"sleep\060\0")
    monkeypatch.setattr("app.tasks.recorder.Path", lambda value: (
        proc if value == "/proc" else Path(value)
    ))

    assert _owned_recorder_pids() == {101}


def test_pid_check_excludes_alert_clip_ffmpeg(monkeypatch) -> None:
    source = (
        b"/usr/bin/ffmpeg\0-i\0rtsp://redacted\0-c\0copy\0-movflags\0"
        b"+frag_keyframe+empty_moov\0/source.mp4\0"
    )
    alert_clip = (
        b"/usr/bin/ffmpeg\0-i\0/source.mp4\0-c:v\0libx264\0"
        b"-movflags\0+faststart\0/alert.mp4\0"
    )
    payload = source

    def _open(_path, _mode):
        return io.BytesIO(payload)

    monkeypatch.setattr("builtins.open", _open)
    assert _pid_is_ffmpeg(101)

    payload = alert_clip
    assert not _pid_is_ffmpeg(202)


def test_stop_all_terminates_untracked_owned_recorders(monkeypatch) -> None:
    signals = []

    class _Redis:
        def scan_iter(self, *, match: str, count: int):
            assert match == "vg:recording:pid:*"
            assert count == 200
            return iter(())

    monkeypatch.setattr(
        "app.tasks.recorder._owned_recorder_pids", lambda: {101},
    )
    monkeypatch.setattr(
        "app.tasks.recorder.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        "app.tasks.recorder.signal.SIGKILL", 9, raising=False,
    )
    monkeypatch.setattr("app.tasks.recorder.time.sleep", lambda _seconds: None)

    _stop_all(_Redis())

    assert signals == [(101, signal.SIGTERM), (101, 9)]


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


def test_window_start_skips_removed_cameras_until_restored(monkeypatch) -> None:
    started = Mock(return_value=True)
    monkeypatch.setattr(recorder, "_start_camera", started)
    queue = Mock()

    with _session() as db:
        retained = Camera(
            name="Retained", brand="generic", connection_type="nvr_channel",
            host="retained.invalid", ai_enabled=False,
        )
        removed = Camera(
            name="Removed", brand="generic", connection_type="nvr_channel",
            host="removed.invalid", ai_enabled=False, is_deleted=True,
        )
        db.add_all([retained, removed])
        db.flush()
        db.add_all([
            Zone(
                camera_id=camera.id, name="Synthetic entrance",
                polygon_coords_json=[], detection_types_json=["entry_exit"],
            )
            for camera in (retained, removed)
        ])
        db.commit()

        assert recorder._start_window(db, queue, "current", 600) == 1
        started.assert_called_once_with(db, queue, retained, "current", 600)

        removed.is_deleted = False
        db.commit()
        started.reset_mock()

        assert recorder._start_window(db, queue, "next", 600) == 2
        assert {call.args[2].id for call in started.call_args_list} == {retained.id, removed.id}


class _RecorderHealthRedis:
    def __init__(self):
        self.values = {
            "vg:recording:pid:1": json.dumps({
                "pid": 101, "window_id": "current", "path": "/clips/1.mp4",
            }),
            "vg:recording:pid:2": json.dumps({
                "pid": 102, "window_id": "current", "path": "/clips/2.mp4",
            }),
            "vg:recording:pid:3": json.dumps({
                "pid": 103, "window_id": "previous", "path": "/clips/3.mp4",
            }),
            "vg:recording:pid:99": json.dumps({
                "pid": 199, "window_id": "current", "path": "/clips/99.mp4",
            }),
        }

    def scan_iter(self, *, match: str, count: int):
        assert match == "vg:recording:pid:*"
        assert count == 200
        return iter(self.values)

    def get(self, key: str):
        return self.values.get(key)

    def exists(self, key: str) -> bool:
        return int(key.rsplit(":", 1)[-1]) in {2, 3}


def test_recording_health_counts_only_expected_live_cameras(monkeypatch) -> None:
    expected_cameras = [
        SimpleNamespace(id=camera_id) for camera_id in range(1, 4)
    ]
    monkeypatch.setattr(
        "app.tasks.recorder._key_cameras", lambda _db: expected_cameras,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._pid_is_ffmpeg", lambda pid: pid in {101, 102, 103, 199},
    )
    monkeypatch.setattr(
        "app.tasks.recorder._recording_output_has_data",
        lambda path: path in {"/clips/1.mp4", "/clips/99.mp4"},
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
        "recorders_missing_with_live_process": 1,
        "cameras_frame_fresh": 2,
        "cameras_frame_stale": 1,
        "recorders_missing_with_fresh_frame": 2,
        "recorders_active_without_fresh_frame": 1,
        "recorder_failure_reasons": {},
        "recorders_missing_without_failure_reason": 2,
        "status": "degraded",
    }
    assert "camera_ids" not in result


def test_recording_output_requires_non_empty_evidence(tmp_path) -> None:
    missing = tmp_path / "missing.mp4"
    empty = tmp_path / "empty.mp4"
    evidence = tmp_path / "evidence.mp4"
    empty.touch()
    evidence.write_bytes(b"fragment")

    assert not _recording_output_has_data("")
    assert not _recording_output_has_data(str(missing))
    assert not _recording_output_has_data(str(empty))
    assert _recording_output_has_data(str(evidence))


def test_recording_output_must_still_be_growing(tmp_path) -> None:
    evidence = tmp_path / "evidence.mp4"
    evidence.write_bytes(b"fragment")
    modified_at = evidence.stat().st_mtime

    assert _recording_output_has_data(
        str(evidence), now=modified_at + 180,
    )
    assert not _recording_output_has_data(
        str(evidence), now=modified_at + 181,
    )


def test_stalled_recorder_is_terminated_before_repair(monkeypatch) -> None:
    signals = []
    sleeps = []

    class _Redis:
        deleted = []

        def get(self, key):
            assert key == "vg:recording:pid:7"
            return json.dumps({"pid": 707})

        def delete(self, key):
            self.deleted.append(key)

    redis = _Redis()
    monkeypatch.setattr("app.tasks.recorder._pid_is_ffmpeg", lambda pid: (pid, 9) not in signals)
    monkeypatch.setattr(
        "app.tasks.recorder.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    if not hasattr(signal, "SIGKILL"):
        # The mocked recorder follows the Linux container's signal contract.
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        "app.tasks.recorder.time.sleep", lambda seconds: sleeps.append(seconds),
    )

    assert _stop_tracked_camera_recorder(redis, 7)
    assert signals == [(707, signal.SIGTERM), (707, 9)]
    assert sleeps == [0.5, 0.1]
    assert redis.deleted == ["vg:recording:pid:7"]


def test_recording_health_aggregates_safe_failure_reasons(monkeypatch) -> None:
    expected_cameras = [
        SimpleNamespace(id=camera_id) for camera_id in range(1, 6)
    ]
    monkeypatch.setattr(
        "app.tasks.recorder._key_cameras", lambda _db: expected_cameras,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._pid_is_ffmpeg", lambda pid: pid == 101,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._recording_output_has_data",
        lambda path: path == "/clips/1.mp4",
    )
    redis = _RecorderHealthRedis()
    redis.values["vg:recording:failure:2"] = "output_path_unavailable"
    redis.values["vg:recording:failure:3"] = "rtsp://should-never-be-exposed"
    redis.values["vg:recording:failure:4"] = "ffmpeg_einval"
    redis.values["vg:recording:failure:5"] = "unsupported_output_codec"

    result = _recording_health_snapshot(
        object(), redis, "current", now=1_000.0,
    )

    assert result["recorder_failure_reasons"] == {
        "output_path_unavailable": 1,
        "unclassified": 1,
        "ffmpeg_einval": 1,
        "unsupported_output_codec": 1,
    }
    assert result["recorders_missing_without_failure_reason"] == 0
    assert "rtsp" not in json.dumps(result)


class _RepairRedis(_RecorderHealthRedis):
    def __init__(self):
        super().__init__()
        self.values.pop("vg:recording:pid:99")
        self.live_frames = {2, 3}
        self.locks = set()
        self.deleted = []

    def exists(self, key: str) -> bool:
        return int(key.rsplit(":", 1)[-1]) in self.live_frames

    def set(self, key: str, value: str, **kwargs) -> bool:
        if key.startswith("vg:recording:repair:"):
            if key in self.locks:
                return False
            assert kwargs == {"nx": True, "ex": 300}
            self.locks.add(key)
            return True
        self.values[key] = value
        return True

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


def test_repairs_only_missing_live_recorders_with_retry_bound(monkeypatch) -> None:
    cameras = [
        SimpleNamespace(id=camera_id, store_id=1) for camera_id in range(1, 4)
    ]
    redis = _RepairRedis()
    closed = []
    started = []
    sleeps = []

    class _Db:
        commits = 0

        def commit(self):
            self.commits += 1

    db = _Db()
    monkeypatch.setattr(
        "app.tasks.recorder._key_cameras", lambda _db: cameras,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._pid_is_ffmpeg", lambda pid: pid == 101,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._recording_output_has_data",
        lambda path: path == "/clips/1.mp4",
    )
    monkeypatch.setattr(
        "app.tasks.recorder._close_stale_camera_recordings",
        lambda _db, camera_id, window_id: closed.append(
            (camera_id, window_id),
        ),
    )
    monkeypatch.setattr(
        "app.tasks.recorder._start_camera",
        lambda _db, _r, camera, window_id, seconds: started.append(
            (camera.id, window_id, seconds),
        ) or True,
    )
    monkeypatch.setattr(
        "app.tasks.recorder.time.sleep", lambda seconds: sleeps.append(seconds),
    )

    assert _repair_missing_recordings(db, redis, "current", 600) == 2
    assert started == [(2, "current", 600), (3, "current", 600)]
    assert closed == [(2, "current"), (3, "current")]
    assert "vg:recording:pid:1" not in redis.deleted
    assert db.commits == 1
    assert sleeps == [5]

    assert _repair_missing_recordings(db, redis, "current", 600) == 0
    assert db.commits == 2
    assert sleeps == [5]


@pytest.mark.parametrize("failure", [PermissionError("private diagnostic"), None])
def test_repair_keeps_tracking_when_source_remains_alive(monkeypatch, caplog, failure):
    redis = _RepairRedis()
    camera = SimpleNamespace(id=2, store_id=1)
    monkeypatch.setattr(recorder, "_key_cameras", lambda _db: [camera])
    monkeypatch.setattr(recorder, "_pid_is_ffmpeg", lambda pid: pid == 102)
    monkeypatch.setattr(recorder, "_recording_output_has_data", lambda _path: False)
    monkeypatch.setattr(recorder.time, "sleep", lambda _seconds: None)
    if not hasattr(signal, "SIGKILL"):
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(recorder.os, "kill", Mock(side_effect=failure))
    start = Mock()
    close = Mock()
    monkeypatch.setattr(recorder, "_start_camera", start)
    monkeypatch.setattr(recorder, "_close_stale_camera_recordings", close)

    assert _repair_missing_recordings(Mock(), redis, "current", 600) == 0

    assert "vg:recording:pid:2" in redis.values
    assert "vg:recording:pid:2" not in redis.deleted
    start.assert_not_called()
    close.assert_not_called()
    assert "private diagnostic" not in caplog.text


@pytest.mark.parametrize(("proof", "ttl", "prior_window", "expected"), [
    ("current", 600, None, 1),
    ("previous", 86_340, "previous", 1),
    ("previous", 85_799, "previous", 0),
    ("previous", -1, "previous", 0),
    ("previous", 86_340, None, 0),
    ("older", 86_340, "previous", 0),
])
def test_repairs_proven_recorder_without_streamer_frame(
    monkeypatch, proof, ttl, prior_window, expected,
) -> None:
    cameras = [
        SimpleNamespace(id=camera_id, store_id=1) for camera_id in range(1, 4)
    ]
    redis = _RepairRedis()
    redis.live_frames = set()
    proof_key = "vg:recording:proven:2"
    redis.values[proof_key] = proof
    redis.ttl = Mock(return_value=ttl)
    started = []

    class _Db:
        def commit(self):
            pass

    monkeypatch.setattr(
        "app.tasks.recorder._key_cameras", lambda _db: cameras,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._pid_is_ffmpeg", lambda pid: pid == 101,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._recording_output_has_data",
        lambda path: path == "/clips/1.mp4",
    )
    monkeypatch.setattr(
        "app.tasks.recorder._close_stale_camera_recordings",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        "app.tasks.recorder._start_camera",
        lambda _db, _r, camera, window_id, seconds: started.append(
            (camera.id, window_id, seconds),
        ) or True,
    )
    monkeypatch.setattr("app.tasks.recorder.time.sleep", lambda _seconds: None)

    assert _repair_missing_recordings(
        _Db(), redis, "current", 600, previous_window_id=prior_window,
    ) == expected
    assert started == ([(2, "current", 600)] if expected else [])
    if prior_window is not None and proof == prior_window:
        redis.ttl.assert_called_once_with(proof_key)
    else:
        redis.ttl.assert_not_called()
    assert _repair_missing_recordings(
        _Db(), redis, "current", 600, previous_window_id=prior_window,
    ) == 0


@pytest.mark.parametrize(("now", "proof_window"), [
    (_at(0, 5), "20260820_2000"),
    (_at(0, 10), None),
    (_at(14), "20260821_0700"),
])
def test_tick_limits_prior_window_proof_to_rollover(monkeypatch, now, proof_window):
    current, _, _ = _current_window(now)
    broker = Mock()
    broker.get.return_value = current
    db = Mock()
    repair = Mock()
    monkeypatch.setattr(recorder.settings, "recording_enabled", True)
    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(recorder, "_redis", lambda: broker)
    monkeypatch.setattr(recorder, "_eat_now", lambda: now)
    monkeypatch.setattr(recorder, "_any_recording_alive", lambda _broker: True)
    monkeypatch.setattr(recorder, "_repair_missing_recordings", repair)
    monkeypatch.setattr(recorder, "_publish_recording_health", Mock())

    recorder.tick.run()

    repair.assert_called_once()
    assert repair.call_args.args[:3] == (db, broker, current)
    assert repair.call_args.kwargs == {"previous_window_id": proof_window}


def test_dead_recorder_row_ends_at_last_provable_file_write(tmp_path) -> None:
    db = _session()
    now = datetime(2026, 10, 2, 6, 30, tzinfo=timezone.utc)
    started = now - timedelta(minutes=10)
    last_write = now - timedelta(minutes=4)
    source = tmp_path / "stopped.mp4"
    source.write_bytes(b"partial recording")
    os.utime(source, (last_write.timestamp(), last_write.timestamp()))
    clip = RecordingClip(
        camera_id=7, window_id="current", file_path=str(source),
        status="recording", started_at=started,
    )
    db.add(clip)
    db.commit()

    assert _close_stale_camera_recordings(
        db, 7, "current", now=now,
    ) == 1
    db.commit()
    db.refresh(clip)

    assert clip.status == "completed"
    assert abs((_as_utc_for_test(clip.ended_at) - last_write).total_seconds()) < 1


def _as_utc_for_test(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@pytest.mark.parametrize(
    ("storage", "write_offset", "end_offset"),
    [("media", -20, -20), ("media", 1, 0), ("empty", -20, -60), ("missing", -20, -60)],
)
def test_close_window_bounds_each_source_to_its_last_write(
    tmp_path, monkeypatch, storage, write_offset, end_offset,
) -> None:
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    db = _session()
    ended = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
    started = ended - timedelta(minutes=60)
    source = tmp_path / "clips" / "20260824_1400" / "1" / "7.mp4"
    source.parent.mkdir(parents=True)
    if storage != "missing":
        source.write_bytes(b"recoverable incident source" if storage == "media" else b"")
        last_write = (ended + timedelta(minutes=write_offset)).timestamp()
        os.utime(source, (last_write, last_write))
    clip = RecordingClip(
        camera_id=7, window_id="20260824_1400", file_path=str(source),
        status="recording", started_at=started,
    )
    db.add(clip)
    db.commit()

    assert _close_window(db, clip.window_id, ended_at=ended) == 1

    db.refresh(clip)
    assert clip.status == "completed"
    assert clip.file_path == str(source)
    assert source.exists() == (storage != "missing")
    assert _as_utc_for_test(clip.ended_at) == ended + timedelta(minutes=end_offset)


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
    expired_end = now - timedelta(hours=13)
    db.add_all([
        RecordingClip(
            camera_id=1, window_id="recent", file_path=str(recent),
            status="completed", ended_at=now - timedelta(hours=11),
        ),
        RecordingClip(
            camera_id=2, window_id="expired", file_path=str(expired),
            status="completed", started_at=expired_end - timedelta(hours=1),
            ended_at=expired_end,
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
    assert _as_utc_for_test(rows["expired"].ended_at) == expired_end

    event = DetectionEvent(camera_id=2, detection_type="intrusion")
    event.timestamp = expired_end - timedelta(minutes=1)
    assert clip_eligibility(db, event) == (True, None)
    event.timestamp = expired_end + timedelta(minutes=1)
    assert clip_eligibility(db, event) == (False, "no_recording_coverage")


def test_source_window_cleanup_retries_a_failed_removal(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    ended = now - timedelta(hours=13)
    window = tmp_path / "clips" / "expired"
    window.mkdir(parents=True)
    source = window / "camera.mp4"
    source.write_bytes(b"test recording")

    with _session() as db:
        clip = RecordingClip(
            camera_id=1, window_id="expired", file_path=str(source),
            started_at=ended - timedelta(hours=1), ended_at=ended, status="completed",
        )
        db.add(clip)
        db.commit()

        def fail_removal(path):
            assert path == window
            raise PermissionError("private filesystem diagnostic")

        with monkeypatch.context() as patch:
            patch.setattr(recorder.shutil, "rmtree", fail_removal)
            assert _prune_expired_source_windows(db, now=now, retention_hours=12) == 0

        db.refresh(clip)
        assert source.exists()
        assert clip.status == "completed"
        assert clip.file_path == str(source)
        assert "source window removal failed" in caplog.text
        assert str(window) not in caplog.text
        assert "private filesystem diagnostic" not in caplog.text

        assert _prune_expired_source_windows(db, now=now, retention_hours=12) == 1
        db.refresh(clip)
        assert not window.exists()
        assert clip.status == "deleted"
        assert clip.file_path is None
        assert _as_utc_for_test(clip.ended_at) == ended


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
