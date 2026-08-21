from streamer.streamer import main as streamer_main


class _HealthBuffer:
    def __init__(self):
        self.updates = []

    def update_health(self, camera_id, *, fps, error):
        self.updates.append({"camera_id": camera_id, "fps": fps,
                             "error": error})


def test_failed_tcp_preflight_is_visible_in_camera_health(monkeypatch) -> None:
    health = _HealthBuffer()

    async def unreachable(_specs):
        return {262: False}

    monkeypatch.setattr(streamer_main, "_health_buffer", health)
    monkeypatch.setattr(streamer_main, "_async_check_all", unreachable)
    monkeypatch.setattr(streamer_main, "RETRY_UNREACHABLE_SECONDS", 300)
    streamer_main._reachable_cache.clear()
    spec = streamer_main.CameraSpec(
        camera_id=262,
        rtsp_url="rtsp://operator:secret@camera.example.invalid:554/live",
    )

    assert streamer_main._filter_reachable([spec]) == []
    assert health.updates == [{
        "camera_id": 262,
        "fps": 0,
        "error": (
            "TCP preflight failed: camera.example.invalid:554 unreachable; "
            "retrying in 300s"
        ),
    }]


def test_health_write_failure_does_not_block_reconciliation(monkeypatch) -> None:
    class BrokenHealthBuffer:
        def update_health(self, *_args, **_kwargs):
            raise RuntimeError("redis unavailable")

    async def unreachable(_specs):
        return {262: False}

    monkeypatch.setattr(streamer_main, "_health_buffer", BrokenHealthBuffer())
    monkeypatch.setattr(streamer_main, "_async_check_all", unreachable)
    streamer_main._reachable_cache.clear()

    assert streamer_main._filter_reachable([
        streamer_main.CameraSpec(
            camera_id=262,
            rtsp_url="rtsp://camera.example.invalid:554/live",
        ),
    ]) == []
