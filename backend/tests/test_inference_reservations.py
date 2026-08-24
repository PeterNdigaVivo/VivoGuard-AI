"""Regression tests for restart-safe, backlog-safe inference reservations."""
from app.tasks import inference


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def eval(self, script, _key_count, lock, heartbeat, task_id, *ttls):
        if script == inference._CLAIM_LUA:
            if self.values.get(lock) != task_id:
                return 0
            self.ttls[lock] = int(ttls[0])
            self.values[heartbeat] = task_id
            self.ttls[heartbeat] = int(ttls[1])
            return 1
        if script == inference._RELEASE_LUA:
            released = 0
            for key in (lock, heartbeat):
                if self.values.get(key) == task_id:
                    self.values.pop(key)
                    self.ttls.pop(key, None)
                    released += 1
            return released
        raise AssertionError("unexpected Lua script")


class FakePipeline:
    def __init__(self, present: set[int], *, fail: bool = False):
        self.present = present
        self.camera_ids: list[int] = []
        self.fail = fail

    def exists(self, key: str):
        self.camera_ids.append(int(key.rsplit(":", 1)[-1]))
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("redis unavailable")
        return [camera_id in self.present for camera_id in self.camera_ids]


class FakeFreshnessRedis:
    def __init__(self, present: set[int], *, fail: bool = False):
        self.pipe = FakePipeline(present, fail=fail)

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return self.pipe


class FakeReservationRedis:
    def __init__(self, present: set[str], queue_depth: int):
        self.present = present
        self.keys = []
        self.queue_depth = queue_depth

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return self

    def exists(self, key: str):
        self.keys.append(key)
        return self

    def execute(self):
        return [key in self.present for key in self.keys]

    def llen(self, queue: str):
        assert queue == "inference"
        return self.queue_depth


def test_only_reserved_task_can_claim_and_claim_shortens_active_lease():
    r = FakeRedis()
    r.values["lock"] = "current-task"
    r.ttls["lock"] = inference.PENDING_LOCK_TTL

    assert not inference._claim_reserved_task(
        r, lock="lock", heartbeat="hb", task_id="stale-task",
    )
    assert "hb" not in r.values

    assert inference._claim_reserved_task(
        r, lock="lock", heartbeat="hb", task_id="current-task",
    )
    assert r.ttls["lock"] == inference.LOCK_TTL
    assert r.values["hb"] == "current-task"


def test_stale_task_cannot_release_newer_task_keys():
    r = FakeRedis()
    r.values.update({"lock": "new-task", "hb": "new-task"})

    assert inference._release_owned_task(
        r, lock="lock", heartbeat="hb", task_id="old-task",
    ) == 0
    assert r.values == {"lock": "new-task", "hb": "new-task"}

    assert inference._release_owned_task(
        r, lock="lock", heartbeat="hb", task_id="new-task",
    ) == 2
    assert r.values == {}


def test_pending_reservation_outlives_full_cpu_queue_rotation():
    # At the eight-slot production default, a 101-camera fail-open queue fits
    # comfortably inside the lease, preventing duplicate reservations.
    worst_case_rotation = ((101 + 8 - 1) // 8) * inference.RUN_SECONDS
    assert inference.PENDING_LOCK_TTL >= worst_case_rotation * 2


def test_inference_queue_preserves_legacy_default_and_distributes_stably():
    assert inference._inference_queue(41, 1) == "inference"
    assert inference._inference_queue(41, 4) == "inference.1"
    assert inference._inference_queue(45, 4) == "inference.1"


def test_inference_queue_rejects_invalid_shard_count():
    import pytest

    with pytest.raises(ValueError, match="at least 1"):
        inference._inference_queue(1, 0)


def test_freshness_gate_only_schedules_cameras_with_live_frames():
    r = FakeFreshnessRedis({2, 7})

    assert inference._camera_ids_with_fresh_frames(r, [1, 2, 7, 9]) == {2, 7}
    assert r.pipe.camera_ids == [1, 2, 7, 9]


def test_freshness_gate_fails_open_when_redis_check_fails():
    r = FakeFreshnessRedis(set(), fail=True)

    assert inference._camera_ids_with_fresh_frames(r, [3, 4]) == {3, 4}


def test_reservation_health_distinguishes_active_from_waiting_tasks():
    r = FakeReservationRedis({
        "vg:inference-lock:1", "vg:inference-lock:2",
        "vg:inference-lock:3", "vg:inference-hb:1",
    }, queue_depth=2)

    assert inference._reservation_health(r, [1, 2, 3]) == {
        "cameras_reserved": 3,
        "cameras_actively_inferencing": 1,
        "cameras_waiting_for_worker": 2,
        "inference_queue_depth": 2,
        "inference_queue_depth_by_shard": {"inference": 2},
        "estimated_full_rotation_seconds": 3 * inference.RUN_SECONDS,
    }
