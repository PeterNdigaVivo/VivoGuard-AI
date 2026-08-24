from streamer.streamer import main as streamer_main


def test_effective_fps_caps_gpu_tuned_camera_on_cpu(monkeypatch) -> None:
    monkeypatch.setattr(streamer_main, "STREAMER_MAX_FPS", 3)

    assert streamer_main._effective_fps(5) == 3
    assert streamer_main._effective_fps(1) == 1
    assert streamer_main._effective_fps(None) >= 1


def test_saved_dahua_mainstream_is_rewritten_to_substream(monkeypatch) -> None:
    monkeypatch.setattr(streamer_main, "PREFER_SUBSTREAM_OVERRIDES", True)

    assert streamer_main._prefer_substream_override(
        "rtsp://operator:secret@example.test/cam/realmonitor?channel=4&subtype=0",
        "dahua",
    ) == (
        "rtsp://operator:secret@example.test/cam/realmonitor?channel=4&subtype=1"
    )


def test_saved_hikvision_mainstream_is_rewritten_to_substream(monkeypatch) -> None:
    monkeypatch.setattr(streamer_main, "PREFER_SUBSTREAM_OVERRIDES", True)

    assert streamer_main._prefer_substream_override(
        "rtsp://operator:secret@example.test/Streaming/Channels/1201",
        "hikvision",
    ) == "rtsp://operator:secret@example.test/Streaming/Channels/1202"


def test_unknown_override_shape_is_preserved(monkeypatch) -> None:
    monkeypatch.setattr(streamer_main, "PREFER_SUBSTREAM_OVERRIDES", True)
    url = "rtsp://example.test/custom/main"

    assert streamer_main._prefer_substream_override(url, "generic") == url


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


def test_endpoint_change_invalidates_negative_reachability_cache(monkeypatch) -> None:
    calls = []

    async def check(specs):
        calls.append([streamer_main._spec_probe_endpoint(spec) for spec in specs])
        return {spec.camera_id: False for spec in specs}

    monkeypatch.setattr(streamer_main, "_async_check_all", check)
    streamer_main._reachable_cache.clear()
    old = streamer_main.CameraSpec(
        camera_id=163, rtsp_url="rtsp://camera.example.invalid:554/live",
    )
    corrected = streamer_main.CameraSpec(
        camera_id=163, rtsp_url="rtsp://camera.example.invalid:8080/live",
    )

    assert streamer_main._filter_reachable([old]) == []
    assert streamer_main._filter_reachable([corrected]) == []
    assert calls == [
        [("camera.example.invalid", 554)],
        [("camera.example.invalid", 8080)],
    ]
