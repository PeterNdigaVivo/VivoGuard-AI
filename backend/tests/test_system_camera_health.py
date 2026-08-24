from app.api.system import _decode_inference_pipeline, _runtime_camera_status


def test_fresh_frames_override_pending_configuration() -> None:
    status, age = _runtime_camera_status(
        "pending",
        {"last_frame_at": 995.0, "fps": 1.2},
        now=1000.0,
    )

    assert status == "online"
    assert age == 5.0


def test_old_frame_is_reported_as_stale() -> None:
    status, age = _runtime_camera_status(
        "online",
        {"last_frame_at": 980.0, "fps": 5.0},
        now=1000.0,
    )

    assert status == "stale"
    assert age == 20.0


def test_stream_error_is_offline_even_when_camera_is_pending() -> None:
    status, age = _runtime_camera_status(
        "pending",
        {"fps": 0, "error": "RTSP OPTIONS failed: 404 Not Found"},
        now=1000.0,
    )

    assert status == "offline"
    assert age is None


def test_pending_without_streamer_evidence_remains_pending() -> None:
    assert _runtime_camera_status("pending", {}, now=1000.0) == (
        "pending", None,
    )


def test_invalid_health_values_fail_closed() -> None:
    assert _runtime_camera_status(
        "online",
        {"last_frame_at": "not-a-timestamp", "fps": "unknown"},
        now=1000.0,
    ) == ("offline", None)


def test_inference_pipeline_telemetry_decodes_only_json_objects() -> None:
    assert _decode_inference_pipeline(b'{"cameras_fresh": 65}') == {
        "cameras_fresh": 65,
    }
    assert _decode_inference_pipeline("not-json") is None
    assert _decode_inference_pipeline("[]") is None
    assert _decode_inference_pipeline(None) is None
