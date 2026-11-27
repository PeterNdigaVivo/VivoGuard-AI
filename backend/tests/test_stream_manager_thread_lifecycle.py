"""The stream manager must replace a worker after its thread exits."""
import pytest

from app.stream.ffmpeg_worker import FFmpegWorker
from app.stream.http_snapshot_worker import HttpSnapshotWorker
from app.stream.manager import CameraSpec, StreamManager


@pytest.mark.parametrize(
    ("transport", "worker_class"),
    [("rtsp", FFmpegWorker), ("http_snapshot", HttpSnapshotWorker)],
)
def test_reconcile_replaces_finished_worker(monkeypatch, transport, worker_class):
    monkeypatch.setattr(worker_class, "run", lambda self: None)
    manager = StreamManager(buffer=object())
    spec = CameraSpec(
        camera_id=1, rtsp_url="rtsp://example.test/stream",
        transport=transport, snapshot_url="http://example.test/snapshot",
    )

    try:
        manager.reconcile([spec])
        finished = manager.workers[spec.camera_id]
        assert isinstance(finished, worker_class)
        finished.join(timeout=2)
        assert not finished.is_alive()

        manager.reconcile([spec])
        replacement = manager.workers[spec.camera_id]
        assert isinstance(replacement, worker_class)
        assert replacement is not finished
        replacement.join(timeout=2)
        assert not replacement.is_alive()
    finally:
        manager.stop_all()
