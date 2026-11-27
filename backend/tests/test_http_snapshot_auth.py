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
