import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import redis

from app.stream import frame_buffer


def test_health_update_sends_only_status_fields_in_one_redis_operation(monkeypatch):
    redis_client = Mock()
    monkeypatch.setattr(frame_buffer, "time", SimpleNamespace(time=lambda: 100.0))

    frame_buffer.FrameBuffer(redis_client).update_health(7, fps=1.234)

    (_, key_count, key, camera_id, raw_patch, ttl), kwargs = redis_client.eval.call_args
    assert not kwargs
    assert (key_count, key, camera_id, ttl) == (1, "vg:health:7", 7, frame_buffer.HEALTH_TTL_SECONDS)
    assert json.loads(raw_patch) == {"fps": 1.23, "last_health_at": 100.0, "error": None}
    redis_client.get.assert_not_called()
    redis_client.set.assert_not_called()


@pytest.fixture
def isolated_redis():
    socket = os.environ.get("VG_FRAME_BUFFER_TEST_SOCKET")
    if not socket:
        pytest.skip("isolated Redis test socket not configured")
    with redis.Redis(unix_socket_path=socket) as client:
        yield client


def test_status_update_preserves_frame_published_during_update(isolated_redis, monkeypatch):
    camera_id = 907
    health_key = f"vg:health:{camera_id}"
    isolated_redis.set(health_key, json.dumps({
        "camera_id": camera_id, "last_frame_at": 50.0, "fps": 0.5, "error": "old status",
    }))
    clock = SimpleNamespace(now=90.0)
    monkeypatch.setattr(frame_buffer, "time", SimpleNamespace(time=lambda: clock.now))
    writer = frame_buffer.FrameBuffer(isolated_redis)
    reader = Mock(wraps=isolated_redis)

    def publish_new_frame():
        clock.now = 100.0
        writer.push_frame(camera_id, b"synthetic-jpeg")

    def legacy_read(key):
        raw = isolated_redis.get(key)
        publish_new_frame()
        return raw

    def atomic_merge(*args):
        publish_new_frame()
        return isolated_redis.eval(*args)

    # Insert a frame after a client-side read, or just before an atomic merge.
    reader.get.side_effect = legacy_read
    reader.eval.side_effect = atomic_merge
    frame_buffer.FrameBuffer(reader).update_health(camera_id, fps=1.2)

    assert writer.health(camera_id) == {
        "camera_id": camera_id,
        "last_frame_at": 100.0,
        "last_health_at": 90.0,
        "fps": 1.2,
        "error": None,
    }
    assert writer.latest_jpeg(camera_id, prefer_overlay=False) == b"synthetic-jpeg"
    assert 0 < isolated_redis.ttl(health_key) <= frame_buffer.HEALTH_TTL_SECONDS


@pytest.mark.parametrize("legacy_health", [b"[]", b"null", b"{broken"])
def test_status_only_repairs_invalid_health_without_claiming_frame(
    isolated_redis, monkeypatch, legacy_health,
):
    camera_id = 908
    isolated_redis.set(f"vg:health:{camera_id}", legacy_health)
    monkeypatch.setattr(frame_buffer, "time", SimpleNamespace(time=lambda: 90.0))
    writer = frame_buffer.FrameBuffer(isolated_redis)

    writer.update_health(camera_id, fps=0.0, error="synthetic offline")

    assert writer.health(camera_id) == {
        "camera_id": camera_id, "last_health_at": 90.0, "fps": 0.0, "error": "synthetic offline",
    }
