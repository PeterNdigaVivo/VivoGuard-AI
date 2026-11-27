import asyncio
import logging
from unittest.mock import Mock

import httpx
import pytest

from streamer.streamer import main as streamer_main


def test_streamer_suppresses_httpx_request_urls(monkeypatch, caplog) -> None:
    client_log = logging.getLogger("httpx")
    caplog.set_level(logging.INFO, logger="httpx")
    caplog.set_level(logging.INFO)
    manager = Mock()

    def startup():
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://camera.invalid/snapshot?token=synthetic-query-marker")
        client_log.warning("synthetic transport warning")
        streamer_main.log.warning("synthetic camera diagnostic")

    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(streamer_main, "_run_migrations", startup)
    monkeypatch.setattr(streamer_main, "StreamManager", lambda: manager)
    monkeypatch.setattr(streamer_main, "desired_specs", interrupt)

    streamer_main.main()

    assert "synthetic-query-marker" not in caplog.text
    assert "synthetic transport warning" in caplog.text
    assert "synthetic camera diagnostic" in caplog.text
    manager.stop_all.assert_called_once()


def test_snapshot_url_keeps_credentials_out_of_httpx_request_url():
    url = streamer_main._snapshot_url_for(
        "camera.example.invalid",
        8080,
        3,
        None,
    )

    assert url == (
        "http://camera.example.invalid:8080/cgi-bin/snapshot.cgi?channel=3"
    )
    assert "@" not in url


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


def test_reachability_cache_retries_failures_before_healthy_endpoints(
    monkeypatch,
) -> None:
    monkeypatch.setattr(streamer_main, "RETRY_UNREACHABLE_SECONDS", 30)
    monkeypatch.setattr(streamer_main, "RECHECK_REACHABLE_SECONDS", 300)

    assert streamer_main._reachability_cache_ttl(False) == 30
    assert streamer_main._reachability_cache_ttl(True) == 300


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


@pytest.mark.parametrize(("alive", "endpoint_changed", "retained"), [
    (True, False, True),
    (False, False, False),
    (True, True, False),
])
def test_preflight_preserves_existing_worker_only_for_unchanged_live_specs(
    monkeypatch, alive, endpoint_changed, retained,
):
    original = streamer_main.CameraSpec(
        camera_id=42, rtsp_url="rtsp://camera.example.invalid:554/live",
    )
    desired = streamer_main.CameraSpec(
        camera_id=42,
        rtsp_url="rtsp://camera.example.invalid:8554/live" if endpoint_changed else original.rtsp_url,
    )
    manager = streamer_main.StreamManager(buffer=object())
    worker = Mock()
    worker.is_alive.return_value = alive

    def stop():
        worker.is_alive.return_value = False

    worker.stop.side_effect = stop
    manager.specs[original.camera_id] = original
    manager.workers[original.camera_id] = worker
    probed = []

    async def failed_probe(specs):
        probed.extend(specs)
        return {spec.camera_id: False for spec in specs}

    health = _HealthBuffer()
    monkeypatch.setattr(streamer_main, "_health_buffer", health)
    monkeypatch.setattr(streamer_main, "_reachable_cache", {})
    monkeypatch.setattr(streamer_main, "_async_check_all", failed_probe)

    eligible = streamer_main._filter_reachable([desired], manager=manager)
    manager.reconcile(eligible)

    if retained:
        assert eligible == [original]
        assert manager.workers[original.camera_id] is worker
        assert probed == []
        assert health.updates == []
        worker.stop.assert_not_called()
    else:
        assert eligible == []
        assert probed == [desired]
        assert len(health.updates) == 1
        assert original.camera_id not in manager.workers
        worker.stop.assert_called_once()


def test_tcp_preflight_checks_each_unique_endpoint_once(monkeypatch):
    spec = streamer_main.CameraSpec
    cameras = [
        spec(camera_id=1, rtsp_url="rtsp://nvr.example.invalid:554/channel/1"),
        spec(camera_id=2, rtsp_url="rtsp://nvr.example.invalid:554/channel/2"),
        spec(camera_id=3, rtsp_url="", transport="http_snapshot",
             snapshot_url="http://nvr.example.invalid:8080/snapshot?channel=1"),
        spec(camera_id=4, rtsp_url="rtsp://other.example.invalid:554/channel/1"),
        spec(camera_id=5, rtsp_url=""),
        spec(camera_id=6, rtsp_url="", transport="http_snapshot",
             snapshot_url="http://nvr.example.invalid:8080/snapshot?channel=2"),
    ]
    attempts = []

    async def check(host, port, timeout):
        assert timeout == streamer_main.TCP_CHECK_TIMEOUT_SECONDS
        attempts.append((host, port))
        return port == 554

    monkeypatch.setattr(streamer_main, "_tcp_open", check)

    assert asyncio.run(streamer_main._async_check_all(cameras)) == {
        1: True, 2: True, 3: False, 4: True, 5: False, 6: False,
    }
    assert sorted(attempts) == [
        ("nvr.example.invalid", 554),
        ("nvr.example.invalid", 8080),
        ("other.example.invalid", 554),
    ]
