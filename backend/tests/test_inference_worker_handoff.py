import json
import os
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest
import redis

from app.tasks import inference


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "inference-worker-handoff.py"
    return runpy.run_path(str(path))["handoff"].__globals__


def test_handoff_releases_only_after_exact_container_stops(module, monkeypatch):
    old = module["Container"]("worker-id", "running", "start", "worker-inference", "test")
    peer = module["Container"]("alerts-id", "running", "start", "worker-alerts", "test")
    states = {old.id: old, peer.id: peer}
    events = []
    snapshot = json.dumps({"redis": ["server", 0], "claims": {"lease": "task"}})

    def docker(*args, input_text=None, **kwargs):
        if args[-1] == "capture":
            return snapshot
        if args[-1] == "identify":
            return '["server", 0]'
        if args[0] == "stop":
            assert args == ("stop", "--time", "45", old.id)
            states[old.id] = old._replace(status="exited")
            events.append("stopped")
            return ""
        assert args[0:3] == ("exec", "-i", peer.id)
        assert args[-1] == "release" and input_text == snapshot
        assert states[old.id].status == "exited"
        events.append("released")
        return "1"

    monkeypatch.setitem(module, "container", states.__getitem__)
    monkeypatch.setitem(module, "docker", docker)
    assert module["handoff"](old.id, peer.id) == 1
    assert events == ["stopped", "released"]


def test_handoff_restarts_prior_worker_if_redis_release_fails(module, monkeypatch):
    old = module["Container"]("worker-id", "running", "start", "worker-inference", "test")
    peer = module["Container"]("alerts-id", "running", "start", "worker-alerts", "test")
    states = {old.id: old, peer.id: peer}
    events = []

    def docker(*args, **kwargs):
        if args[-1] == "capture":
            return '{"redis": ["server", 0], "claims": {}}'
        if args[-1] == "identify":
            return '["server", 0]'
        if args[0] == "stop":
            states[old.id] = old._replace(status="exited")
            events.append("stopped")
        elif args[0] == "start":
            assert args[1] == old.id
            events.append("restarted")
        else:
            raise module["HandoffError"]("Redis changed")

    monkeypatch.setitem(module, "container", states.__getitem__)
    monkeypatch.setitem(module, "docker", docker)
    with pytest.raises(module["HandoffError"], match="Redis changed"):
        module["handoff"](old.id, peer.id)
    assert events == ["stopped", "restarted"]


def test_capture_requires_a_single_consumer_and_exact_celery_ownership(module, monkeypatch):
    monkeypatch.setattr(module["socket"], "gethostname", lambda: "test")
    control = Mock()
    node = "inference-worker@test"
    routes = {node: [{"name": "inference"}, {"name": "default"}]}
    inspector = control.inspect.return_value
    inspector.active_queues.return_value = routes
    inspector.active.return_value = {
        node: [{"name": "inference.run_camera", "args": [7], "id": "expected"}],
    }
    lock = inference.LOCK_KEY_FMT.format(camera_id=7)
    claims = {lock: "expected"}
    monkeypatch.setitem(module, "long_claims", lambda *_: claims)
    assert module["capture"](Mock(), inference, control) == claims

    claims[lock] = "newer"
    with pytest.raises(module["HandoffError"], match="not proven"):
        module["capture"](Mock(), inference, control)

    routes["another-worker"] = [{"name": "inference"}]
    with pytest.raises(module["HandoffError"], match="single local inference consumer"):
        module["capture"](Mock(), inference, control)


def test_unsnapshotted_active_claim_aborts_before_any_release(module, monkeypatch):
    lease = {inference.LOCK_KEY_FMT.format(camera_id=7): "unrecognized"}
    monkeypatch.setitem(module, "long_claims", lambda *_: lease)
    client = Mock()

    with pytest.raises(module["HandoffError"], match="claims changed"):
        module["release"](client, inference, {})

    client.eval.assert_not_called()


def test_redis_release_preserves_a_successor_that_claimed_after_snapshot(module, monkeypatch):
    socket = os.environ.get("VG_FRAME_BUFFER_TEST_SOCKET")
    if not socket:
        pytest.skip("isolated Redis test socket not configured")
    with redis.Redis(unix_socket_path=socket, decode_responses=True) as client:
        old_lock = inference.LOCK_KEY_FMT.format(camera_id=907)
        old_heartbeat = inference.HB_KEY_FMT.format(camera_id=907)
        new_lock = inference.LOCK_KEY_FMT.format(camera_id=908)
        new_heartbeat = inference.HB_KEY_FMT.format(camera_id=908)
        for key in (old_lock, old_heartbeat, new_lock, new_heartbeat):
            client.set(key, "old", ex=inference.LOCK_TTL + 1_800)
        eval_script = client.eval

        def race(script, numkeys, lock, heartbeat, owner):
            if lock == new_lock:
                client.mset({new_lock: "successor", new_heartbeat: "successor"})
            return eval_script(script, numkeys, lock, heartbeat, owner)

        monkeypatch.setattr(client, "eval", race)
        assert module["release"](client, inference, {old_lock: "old", new_lock: "old"}) == 1
        assert client.mget(old_lock, old_heartbeat, new_lock, new_heartbeat) == [
            None, None, "successor", "successor",
        ]
