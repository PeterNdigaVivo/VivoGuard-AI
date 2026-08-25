"""Regression tests for restart-safe, backlog-safe inference reservations."""
from types import SimpleNamespace

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
        return self.queue_depth if queue == "inference" else 0


class FakeLastRunRedis:
    def __init__(self, values: dict[int, str | None]):
        self.values = values
        self.keys: list[str] = []

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return self

    def get(self, key: str):
        self.keys.append(key)
        return self

    def execute(self):
        return [self.values.get(int(key.rsplit(":", 1)[-1])) for key in self.keys]


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
        "inference_shards": {
            "inference": {
                "cameras": 3,
                "reserved": 3,
                "active": 1,
                "queue_depth": 2,
            },
        },
        "estimated_full_rotation_seconds": 3 * inference.RUN_SECONDS,
    }


def test_latency_critical_camera_gets_short_priority_slice():
    camera = SimpleNamespace(
        detection_configs=[SimpleNamespace(
            enabled=True, detection_type="intrusion",
        )],
        zones=[],
    )
    ordinary = SimpleNamespace(
        detection_configs=[SimpleNamespace(
            enabled=True, detection_type="dwell",
        )],
        zones=[],
    )

    assert inference._task_profile(camera) == (
        inference.CRITICAL_RUN_SECONDS, 0,
    )
    assert inference._task_profile(ordinary) == (inference.RUN_SECONDS, 9)


def test_unsuppressed_critical_zone_enters_fast_lane():
    camera = SimpleNamespace(
        detection_configs=[],
        zones=[SimpleNamespace(
            suppressed=False,
            detection_types_json=["person", "entry_exit"],
        )],
    )

    assert inference._camera_is_latency_critical(camera)


def test_critical_camera_cooldown_prevents_normal_work_starvation():
    now = 1000.0

    assert not inference._critical_due_from_timestamp(
        str(now - inference.CRITICAL_REQUEUE_SECONDS + 1), now=now,
    )
    assert inference._critical_due_from_timestamp(
        str(now - inference.CRITICAL_REQUEUE_SECONDS), now=now,
    )
    assert inference._critical_due_from_timestamp(None, now=now)


def test_critical_requeue_budget_preserves_non_preemptive_sla_headroom():
    assert inference._critical_gap_budget_seconds() == (
        inference.CRITICAL_REQUEUE_SECONDS
        + inference.SUPERVISOR_INTERVAL_SECONDS
        + inference.RUN_SECONDS
        + inference.CRITICAL_RUN_SECONDS
    )
    assert inference._critical_gap_headroom_seconds() >= (
        inference.CRITICAL_GAP_HEADROOM_SECONDS
    )
    assert inference._critical_gap_budget_seconds() < (
        inference.CRITICAL_GAP_SLA_SECONDS
    )


def test_supervisor_publishes_critical_cameras_before_ordinary_cameras():
    def camera(camera_id: int, detection_type: str):
        return SimpleNamespace(
            id=camera_id,
            detection_configs=[SimpleNamespace(
                enabled=True, detection_type=detection_type,
            )],
            zones=[],
        )

    ordered = inference._schedule_order([
        camera(1, "dwell"),
        camera(2, "intrusion"),
        camera(3, "person"),
        camera(4, "fire"),
    ])

    assert [row.id for row in ordered] == [2, 4, 1, 3]


def test_supervisor_prioritises_oldest_and_never_started_critical_cameras():
    def camera(camera_id: int, detection_type: str):
        return SimpleNamespace(
            id=camera_id,
            detection_configs=[SimpleNamespace(
                enabled=True, detection_type=detection_type,
            )],
            zones=[],
        )

    ordered = inference._schedule_order(
        [
            camera(1, "dwell"),
            camera(2, "intrusion"),
            camera(3, "fire"),
            camera(4, "entry_exit"),
        ],
        {2: 950.0, 3: None, 4: 700.0},
    )

    assert [row.id for row in ordered] == [3, 4, 2, 1]


def test_supervisor_rotates_ordinary_cameras_oldest_first():
    def camera(camera_id: int):
        return SimpleNamespace(
            id=camera_id,
            detection_configs=[SimpleNamespace(
                enabled=True, detection_type="dwell",
            )],
            zones=[],
        )

    ordered = inference._schedule_order(
        [camera(1), camera(2), camera(3)],
        {1: 950.0, 2: None, 3: 700.0},
    )

    assert [row.id for row in ordered] == [2, 3, 1]


def test_last_run_batch_read_normalises_invalid_values_and_fails_open():
    r = FakeLastRunRedis({1: "900", 2: "invalid", 3: None})

    assert inference._last_run_timestamps(r, [1, 2, 3]) == {
        1: 900.0,
        2: None,
        3: None,
    }


def test_critical_gap_health_uses_actual_starts_and_flags_never_started():
    r = FakeLastRunRedis({1: "900", 2: "600", 3: None})

    health = inference._critical_gap_health(r, [1, 2, 3], now=1000)

    assert health == {
        "critical_cameras_total": 3,
        "critical_cameras_overdue": 2,
        "critical_camera_ids_overdue": [2, 3],
        "critical_cameras_never_started": 1,
        "critical_max_gap_seconds": 400.0,
        "critical_gap_sla_seconds": inference.CRITICAL_GAP_SLA_SECONDS,
        "critical_requeue_seconds": inference.CRITICAL_REQUEUE_SECONDS,
        "critical_gap_budget_seconds": inference._critical_gap_budget_seconds(),
        "critical_gap_headroom_seconds": (
            inference._critical_gap_headroom_seconds()
        ),
    }


def test_standard_gap_health_uses_actual_starts_and_flags_never_started():
    r = FakeLastRunRedis({1: "900", 2: "50", 3: None})

    health = inference._standard_gap_health(r, [1, 2, 3], now=1000)

    assert health == {
        "standard_cameras_total": 3,
        "standard_cameras_overdue": 2,
        "standard_camera_ids_overdue": [2, 3],
        "standard_cameras_never_started": 1,
        "standard_max_gap_seconds": 950.0,
        "standard_gap_sla_seconds": inference.STANDARD_GAP_SLA_SECONDS,
    }
