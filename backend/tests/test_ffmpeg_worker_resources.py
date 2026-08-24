import shlex

from app.stream.ffmpeg_worker import FFmpegWorker, _build_cmd


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
