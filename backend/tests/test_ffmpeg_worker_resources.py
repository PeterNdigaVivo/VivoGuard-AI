import shlex
from unittest.mock import Mock

from app.stream.ffmpeg_worker import (
    FFmpegWorker,
    _build_argv,
    _build_cmd,
    _kill_tree,
    _retry_url_after_failure,
    _stream_has_stalled,
)


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
    assert worker._stop.is_set()


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


def test_stall_requires_a_frame_from_the_current_run() -> None:
    assert not _stream_has_stalled(
        last_frame_at=99, started_at=100, now=140, stall_seconds=20,
    )
    assert not _stream_has_stalled(
        last_frame_at=125, started_at=100, now=140, stall_seconds=20,
    )
    assert _stream_has_stalled(
        last_frame_at=120, started_at=100, now=140, stall_seconds=20,
    )


def test_spawn_uses_argv_so_no_shell_sits_between_us_and_ffmpeg() -> None:
    argv = _build_argv("rtsp://user:pass@example.test:554/s", fps=5, threads=1)

    assert isinstance(argv, list)
    assert argv[0] == "ffmpeg"
    # The URL is one argument, not a quoted fragment of a shell string:
    # that is what lets us spawn without shell=True.
    assert "rtsp://user:pass@example.test:554/s" in argv
    assert not any(" " in part for part in argv if part.startswith("rtsp://"))


def test_kill_tree_falls_back_to_plain_kill_for_an_unresolvable_pid() -> None:
    # A Mock pid cannot be resolved to a process group; the child must
    # still be signalled exactly once.
    proc = Mock()
    proc.pid = Mock()

    _kill_tree(proc)

    proc.kill.assert_called_once_with()


def test_kill_tree_survives_a_child_that_exits_mid_kill() -> None:
    proc = Mock()
    proc.pid = Mock()
    proc.kill.side_effect = OSError("No such process")

    _kill_tree(proc)        # must not raise

    proc.kill.assert_called_once_with()


def test_stderr_drain_keeps_only_the_tail() -> None:
    proc = Mock()
    proc.stderr.read.side_effect = [b"x" * 8192] * 12 + [b""]
    collected: list[bytes] = []

    FFmpegWorker._drain_stderr(proc, collected)

    # Bounded: a camera that logs a warning per frame for hours must not
    # grow the worker's memory without limit.
    assert len(collected) <= 8
