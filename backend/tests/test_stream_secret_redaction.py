import asyncio
import logging

from app.connectors import rtsp
from app.stream import auto_transport
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
