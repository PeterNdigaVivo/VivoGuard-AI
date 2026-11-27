from unittest.mock import Mock

import httpx
import pytest

from app.stream.http_snapshot_worker import HttpSnapshotWorker


JPEG = b"\xff\xd8\xffsynthetic"
DIGEST_CHALLENGE = 'Digest realm="synthetic", nonce="test-nonce", qop="auth"'


@pytest.mark.parametrize(("fallback_status", "fallback_body", "error"), [
    (403, b"rejected", "HTTP 403"),
    (200, b"<html>login</html>", "response not a JPEG"),
])
def test_failed_basic_fallback_does_not_block_later_digest_recovery(
    fallback_status, fallback_body, error,
):
    schemes = []
    digest_ready = False

    def endpoint(request):
        scheme = request.headers.get("Authorization", "").partition(" ")[0]
        schemes.append(scheme)
        if scheme == "Digest" and digest_ready:
            return httpx.Response(200, content=JPEG)
        if scheme == "Basic":
            return httpx.Response(fallback_status, content=fallback_body)
        return httpx.Response(401, headers={"WWW-Authenticate": DIGEST_CHALLENGE})

    worker = HttpSnapshotWorker(
        7, "https://camera.invalid/snapshot",
        username="test-user", password="test-password", buffer=Mock(),
    )
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        worker._client = client
        with pytest.raises(RuntimeError, match=error):
            worker._get_jpeg()
        digest_ready = True
        assert worker._get_jpeg() == JPEG

    assert schemes == ["", "Digest", "Basic", "Digest"]


def test_successful_basic_fallback_is_reused_on_next_snapshot():
    schemes = []

    def endpoint(request):
        scheme = request.headers.get("Authorization", "").partition(" ")[0]
        schemes.append(scheme)
        if scheme == "Basic":
            return httpx.Response(200, content=JPEG)
        return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="synthetic"'})

    worker = HttpSnapshotWorker(
        8, "https://camera.invalid/snapshot",
        username="test-user", password="test-password", buffer=Mock(),
    )
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        worker._client = client
        assert worker._get_jpeg() == JPEG
        assert worker._get_jpeg() == JPEG

    assert schemes == ["", "Basic", "Basic"]


@pytest.mark.parametrize("status", [400, 403])
def test_http_error_body_stays_out_of_camera_diagnostics(status, caplog):
    buffer = Mock()
    worker = HttpSnapshotWorker(9, "https://camera.invalid/snapshot", buffer=buffer)
    body = "synthetic-session-detail"

    def stop_after_error(_camera_id, *, fps, error=None):
        if error and error.startswith("HTTP snapshot:"):
            worker._stop_event.set()

    buffer.update_health.side_effect = stop_after_error
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, text=body))
    with httpx.Client(transport=transport) as client:
        worker._client = client
        worker.run()

    assert f"HTTP snapshot failed: HTTP {status}" in caplog.text
    assert body not in caplog.text
    buffer.update_health.assert_called_with(9, fps=0, error=f"HTTP snapshot: HTTP {status}")
