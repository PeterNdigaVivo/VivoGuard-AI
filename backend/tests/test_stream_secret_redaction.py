import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from app.api import cameras as cameras_api
from app.connectors import rtsp
from app.stream import auto_transport
from app.stream.http_snapshot_worker import HttpSnapshotWorker
from app.stream.manager import StreamManager
from app.schemas.camera import (
    CameraOut,
    NVRChannelOut,
    TestConnectionIn as CameraConnectionIn,
    TestConnectionOut as CameraConnectionOut,
)
from app.utils.stream_secrets import (
    redact_stream_credentials,
    redact_stream_structure,
)


RAW_URL = "rtsp://camera-admin:p%40ss%23word@example.test:554/live"


def test_redacts_url_userinfo_and_credential_query_parameters():
    value = (
        f"open failed: {RAW_URL}?username=camera-admin&password=p%40ss%23word"
    )

    safe = redact_stream_credentials(value)

    assert "camera-admin" not in safe
    assert "p%40ss%23word" not in safe
    assert "rtsp://****:****@example.test:554/live" in safe
    assert "username=****" in safe
    assert "password=****" in safe


def test_stream_manager_hides_both_camera_username_and_password():
    safe = StreamManager._redact(
        "rtsp://camera-operator:secret@example.invalid:554/live"
    )

    assert safe == "rtsp://****:****@example.invalid:554/live"
    assert "camera-operator" not in safe
    assert "secret" not in safe


def test_http_snapshot_worker_hides_credentials_from_url():
    worker = HttpSnapshotWorker(1, RAW_URL)

    assert worker._redacted_url() == (
        "rtsp://****:****@example.test:554/live"
    )


def test_camera_response_schemas_never_serialize_embedded_credentials():
    camera = CameraOut(
        id=1,
        name="Camera",
        site=None,
        brand="dahua",
        connection_type="nvr_dahua",
        host="example.test",
        public_ip=None,
        rtsp_port=554,
        http_port=80,
        nvr_id=None,
        channel_number=1,
        network_type="wan",
        ai_model_id=None,
        ai_enabled=True,
        inference_fps=1,
        status="online",
        last_seen_at=None,
        last_error=None,
        snapshot_url_override=RAW_URL,
        created_at=datetime.now(timezone.utc),
    )
    connection = CameraConnectionOut(ok=True, rtsp_url=RAW_URL)
    channel = NVRChannelOut(
        channel=1,
        name="Channel 1",
        rtsp_main=RAW_URL,
        rtsp_sub=RAW_URL,
    )

    serialized = str({
        "camera": camera.model_dump(mode="json"),
        "connection": connection.model_dump(mode="json"),
        "channel": channel.model_dump(mode="json"),
    })

    assert "camera-admin" not in serialized
    assert "p%40ss%23word" not in serialized
    assert serialized.count("rtsp://****:****@example.test:554/live") == 4


def test_camera_api_never_returns_raw_stream_credentials(monkeypatch):
    async def successful_probe(_url, timeout):
        return True, None

    async def thumbnail(_url, timeout):
        return "jpeg"

    monkeypatch.setattr(cameras_api, "probe_rtsp", successful_probe)
    monkeypatch.setattr(cameras_api, "grab_thumbnail", thumbnail)
    response = asyncio.run(cameras_api.test_connection(
        CameraConnectionIn(
            brand="generic",
            connection_type="generic",
            host="example.test",
            rtsp_url_override=RAW_URL,
        ),
        _u=SimpleNamespace(),
    ))

    camera = SimpleNamespace(
        id=1,
        brand="dahua",
        host="example.test",
        rtsp_port=554,
        username="camera-admin",
        channel_number=1,
        rtsp_url_override=RAW_URL,
        password_encrypted="encrypted",
    )
    db = SimpleNamespace(get=lambda _model, _camera_id: camera)
    monkeypatch.setattr("app.utils.crypto.decrypt", lambda _value: "secret")
    stream = cameras_api.stream_url(
        1,
        subtype=0,
        db=db,
        _u=SimpleNamespace(),
    )

    assert response.rtsp_url == "rtsp://****:****@example.test:554/live"
    assert stream == {"rtsp_url": "rtsp://****:****@example.test:554/live"}


def test_recursively_redacts_operator_diagnostics(monkeypatch):
    monkeypatch.setattr(auto_transport.time, "time", lambda: 123.0)
    auto_transport._record_diagnostic(
        "example.test",
        554,
        {"attempts": [{"url": RAW_URL, "reason": f"failed: {RAW_URL}"}]},
    )

    record = auto_transport.last_diagnostic("example.test", 554)

    assert record["timestamp"] == 123.0
    assert "camera-admin" not in str(record)
    assert "p%40ss%23word" not in str(record)


def test_probe_rtsp_returns_and_logs_only_redacted_errors(monkeypatch, caplog):
    async def failed_run(_cmd, timeout):
        return 1, "", f"{RAW_URL}: Input/output error"

    monkeypatch.setattr(rtsp, "_run", failed_run)
    caplog.set_level(logging.INFO)

    ok, error = asyncio.run(rtsp.probe_rtsp(RAW_URL))

    assert ok is False
    assert "camera-admin" not in error
    assert "p%40ss%23word" not in error
    assert "camera-admin" not in caplog.text
    assert "p%40ss%23word" not in caplog.text


def test_recursive_redactor_preserves_non_secret_values():
    value = {"ok": True, "ports": [554, 8080], "detail": (RAW_URL,)}

    safe = redact_stream_structure(value)

    assert safe["ok"] is True
    assert safe["ports"] == [554, 8080]
    assert safe["detail"] == ("rtsp://****:****@example.test:554/live",)
