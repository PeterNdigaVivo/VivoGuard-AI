import shlex
import subprocess
import sys
from unittest.mock import Mock

from app.stream.ffmpeg_worker import (
    FFmpegWorker,
    _build_cmd,
    _retry_url_after_failure,
    _spawn_ffmpeg,
    _stream_has_stalled,
)
from app.stream.http_snapshot_worker import HttpSnapshotWorker


def test_ffmpeg_command_caps_decode_filter_and_encode_threads() -> None:
    command = _build_cmd(
        "rtsp://user:password@example.test:554/stream",
        fps=3,
        threads=1,
    )
    args = shlex.split(command)

    input_index = args.index("-i")
    output_index = args.index("-f")
    thread_indexes = [i for i, value in enumerate(args) if value == "-threads"]

    assert len(thread_indexes) == 2
    assert thread_indexes[0] < input_index
    assert thread_indexes[1] < output_index
    assert args[thread_indexes[0] + 1] == "1"
    assert args[thread_indexes[1] + 1] == "1"
    assert args[args.index("-filter_threads") + 1] == "1"


def test_ffmpeg_is_spawned_directly_without_orphaning_shell(monkeypatch) -> None:
    popen = Mock(return_value=Mock())
    monkeypatch.setattr("app.stream.ffmpeg_worker.subprocess.Popen", popen)
    command = _build_cmd(
        "rtsp://user:password@example.test:554/path with space",
        fps=3,
    )

    result = _spawn_ffmpeg(command)

    assert result is popen.return_value
    argv = popen.call_args.args[0]
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-i") + 1] == (
        "rtsp://user:password@example.test:554/path with space"
    )
    assert popen.call_args.kwargs["shell"] is False


def test_ffmpeg_worker_uses_configurable_thread_cap(monkeypatch) -> None:
    monkeypatch.setenv("STREAMER_FFMPEG_THREADS", "2")

    worker = FFmpegWorker(1, "rtsp://example.test/stream")

    assert worker.threads == 2


def test_ffmpeg_thread_cap_never_accepts_zero() -> None:
    command = _build_cmd("rtsp://example.test/stream", fps=1, threads=0)

    assert command.count("-threads 1") == 2


def test_stop_kills_active_ffmpeg_to_unblock_worker() -> None:
    worker = FFmpegWorker(1, "rtsp://camera/stream")
    proc = Mock()
    proc.poll.return_value = None
    worker._own_proc(proc)

    worker.stop()

    proc.kill.assert_called_once_with()
    assert worker._stop_event.is_set()


def test_completed_workers_preserve_thread_lifecycle_methods() -> None:
    workers = [
        FFmpegWorker(1, "rtsp://camera/stream"),
        HttpSnapshotWorker(2, "http://camera/snapshot"),
    ]
    for worker in workers:
        worker.run = lambda: None
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()


def test_proc_spawned_after_stop_is_killed_immediately() -> None:
    worker = FFmpegWorker(1, "rtsp://camera/stream")
    proc = Mock()
    proc.poll.return_value = None
    worker.stop()

    worker._own_proc(proc)

    proc.kill.assert_called_once_with()


def test_unavailable_preferred_substream_falls_back_to_saved_mainstream() -> None:
    assert _retry_url_after_failure(
        active_url="rtsp://camera/sub",
        preferred_url="rtsp://camera/sub",
        fallback_url="rtsp://camera/main",
        frames_received=0,
    ) == "rtsp://camera/main"


def test_preferred_substream_exit_falls_back_even_after_receiving_frames() -> None:
    assert _retry_url_after_failure(
        active_url="rtsp://camera/sub",
        preferred_url="rtsp://camera/sub",
        fallback_url="rtsp://camera/main",
        frames_received=1,
    ) == "rtsp://camera/main"


def test_stall_covers_startup_silence_and_post_frame_silence() -> None:
    assert not _stream_has_stalled(
        last_frame_at=99, started_at=100, now=119, stall_seconds=20,
    )
    assert _stream_has_stalled(
        last_frame_at=99, started_at=100, now=120, stall_seconds=20,
    )
    assert not _stream_has_stalled(
        last_frame_at=125, started_at=100, now=140, stall_seconds=20,
    )
    assert _stream_has_stalled(
        last_frame_at=120, started_at=100, now=140, stall_seconds=20,
    )


def test_ffmpeg_can_emit_frames_after_writing_more_than_a_stderr_pipe(monkeypatch) -> None:
    frame = b"\xff\xd8synthetic\xff\xd9"
    program = (
        "import sys\n"
        "sys.stderr.buffer.write(b'x' * (256 * 1024))\n"
        "sys.stderr.buffer.flush()\n"
        f"sys.stdout.buffer.write({frame!r})\n"
        "sys.stdout.buffer.flush()\n"
    )
    buffer = Mock()
    worker = FFmpegWorker(1, "rtsp://camera.invalid/stream", buffer=buffer)
    buffer.push_frame.side_effect = lambda *_: worker._stop_event.set()
    with subprocess.Popen(
        [sys.executable, "-B", "-c", program],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    ) as proc:
        monkeypatch.setattr("app.stream.ffmpeg_worker._spawn_ffmpeg", lambda _cmd: proc)
        worker.start()
        try:
            worker.join(timeout=4)
            buffer.push_frame.assert_called_once_with(1, frame)
            assert not worker.is_alive()
            assert proc.poll() is not None
        finally:
            worker.stop()
            worker.join(timeout=5)


def test_ffmpeg_kills_child_when_stdout_closes_before_exit(monkeypatch) -> None:
    worker = FFmpegWorker(1, "rtsp://camera.invalid/stream", buffer=Mock())
    proc = Mock()
    proc.poll.return_value = None
    proc.stdout.read.return_value = b""
    proc.stderr.read.return_value = b""
    proc.kill.side_effect = worker._stop_event.set
    monkeypatch.setattr("app.stream.ffmpeg_worker._spawn_ffmpeg", lambda _: proc)

    worker.run()

    proc.kill.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=3)
    proc.stderr.read.assert_called_once_with(8192)


def test_ffmpeg_drops_an_incomplete_stderr_line_before_reporting_it(monkeypatch, caplog) -> None:
    buffer = Mock()
    worker = FFmpegWorker(1, "rtsp://camera.invalid/stream", buffer=buffer)
    proc = Mock()
    proc.poll.return_value = 1
    proc.stdout.read.return_value = b""
    proc.stderr.read.side_effect = [
        b"rtsp://synthetic-user:", b"synthetic-partial-detail" * 7,
        *([b"z"] * 6), b"\nsynthetic decoder error\n", b"",
    ]

    def stop_after_error(_camera_id, *, fps, error=None):
        if error == "synthetic decoder error":
            worker._stop_event.set()

    buffer.update_health.side_effect = stop_after_error
    monkeypatch.setattr("app.stream.ffmpeg_worker._spawn_ffmpeg", lambda _: proc)
    monkeypatch.setattr(worker._stop_event, "wait", lambda _: True)

    worker.run()

    buffer.update_health.assert_called_with(1, fps=0, error="synthetic decoder error")
    assert "synthetic-partial-detail" not in caplog.text
