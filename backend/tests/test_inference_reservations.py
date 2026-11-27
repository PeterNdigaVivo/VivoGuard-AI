"""Regression tests for restart-safe, backlog-safe inference reservations."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.tasks import inference


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.queue_depths: dict[str, int] = {}

    def llen(self, key: str) -> int:
        return self.queue_depths.get(key, 0)

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
        if script == inference._RELEASE_STALLED_PENDING_LUA:
            if self.values.get(lock) != task_id:
                return 0
            if heartbeat in self.values:
                return 0
            ttl = self.ttls.get(lock, -2)
            if ttl > int(ttls[0]) and ttl <= int(ttls[1]):
                self.values.pop(lock, None)
                self.ttls.pop(lock, None)
                return 1
            return 0
        raise AssertionError("unexpected Lua script")

    def get(self, key: str):
        return self.values.get(key)


    def set(self, key: str, value: str, *, ex: int, nx: bool = False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True


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
    def __init__(self, values: dict[int, str | None], *, samples=None):
        self.values = values
        self.samples = samples or {}
        self.keys: list[str] = []

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return self

    def get(self, key: str):
        self.keys.append(key)
        return self

    def execute(self):
        keys = self.keys
        self.keys = []
        return [
            (self.samples if key.startswith("vg:perf:") else self.values).get(int(key.rsplit(":", 1)[-1]))
            for key in keys
        ]


def test_stale_lock_sweep_rechecks_heartbeat_and_ttl_at_deletion():
    class SweepRedis(FakeRedis):
        def scan_iter(self, *, match, count):
            assert match == "vg:inference-lock:*"
            assert count == 200
            return iter(tuple(self.values))

        def eval(self, script, key_count, lock, heartbeat, max_ttl):
            assert script == inference._SWEEP_STALE_LUA
            assert key_count == 2
            assert max_ttl == inference.SWEEP_TTL_THRESHOLD_S
            ttl = self.ttls[lock]
            if lock.endswith(":2"):
                self.values[heartbeat] = self.values[lock]
            if heartbeat in self.values or not 0 <= ttl <= max_ttl:
                return -1
            self.values.pop(lock)
            return ttl

    redis = SweepRedis()
    keys = {
        i: inference.LOCK_KEY_FMT.format(camera_id=i) for i in range(1, 6)
    }
    redis.values = {key: "task" for key in keys.values()}
    redis.ttls = {
        keys[1]: 0, keys[2]: 10, keys[3]: 30, keys[4]: 31, keys[5]: -1,
    }

    assert inference._sweep_stale_locks(redis) == 2
    assert keys[1] not in redis.values and keys[3] not in redis.values
    assert all(keys[i] in redis.values for i in (2, 4, 5))
    assert inference.HB_KEY_FMT.format(camera_id=2) in redis.values


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


def test_pinned_camera_gets_long_slice_between_critical_and_ordinary():
    pinned = SimpleNamespace(
        detection_configs=[SimpleNamespace(
            enabled=True, detection_type="dwell",
        )],
        zones=[], always_on_ai=True, id=7,
    )
    critical_and_pinned = SimpleNamespace(
        detection_configs=[SimpleNamespace(
            enabled=True, detection_type="intrusion",
        )],
        zones=[], always_on_ai=True, id=8,
    )

    from app.config import settings
    assert inference._task_profile(pinned) == (
        settings.inference_pinned_slice_seconds, 1,
    )
    # The critical tier always wins: pinning must never stretch a
    # latency-critical camera's slice.
    assert inference._task_profile(critical_and_pinned) == (
        inference.CRITICAL_RUN_SECONDS, 0,
    )


def test_supervisor_publishes_pinned_cameras_between_critical_and_rest():
    def camera(camera_id: int, detection_type: str, pinned: bool = False):
        return SimpleNamespace(
            id=camera_id,
            detection_configs=[SimpleNamespace(
                enabled=True, detection_type=detection_type,
            )],
            zones=[], always_on_ai=pinned,
        )

    ordered = inference._schedule_order([
        camera(1, "dwell"),
        camera(2, "dwell", pinned=True),
        camera(3, "intrusion"),
        camera(4, "dwell", pinned=True),
    ])

    assert [row.id for row in ordered] == [3, 2, 4, 1]


@pytest.mark.parametrize("publish_state", ["claimed", "finished", "failed", "claimed_then_failed"])
@pytest.mark.parametrize("lost_reservation", [False, True])
def test_supervisor_reserves_exact_task_before_publish(monkeypatch, publish_state, lost_reservation):
    redis = FakeRedis()
    lock = inference.LOCK_KEY_FMT.format(camera_id=7)
    heartbeat = inference.HB_KEY_FMT.format(camera_id=7)
    if lost_reservation:
        redis.values[lock] = "superseded-task"
        redis.ttls[lock] = inference.PENDING_LOCK_TTL - inference.STANDARD_GAP_SLA_SECONDS
    camera = SimpleNamespace(id=7, detection_configs=[], zones=[])
    session = MagicMock()
    query = session.__enter__.return_value.query.return_value
    query.options.return_value.filter.return_value.all.return_value = [camera]
    monkeypatch.setattr("app.database.SessionLocal", lambda: session)
    monkeypatch.setattr(inference, "_redis", lambda: redis)
    monkeypatch.setattr(inference, "_sweep_stale_locks", lambda _redis: 0)
    monkeypatch.setattr(inference, "_camera_ids_with_fresh_frames", lambda *_: {7})
    monkeypatch.setattr(inference, "_last_run_timestamps", lambda *_: {7: None})
    monkeypatch.setattr(inference, "_reservation_health", lambda *_: {})
    monkeypatch.setattr(inference, "_critical_gap_health", lambda *_: {})
    monkeypatch.setattr(inference, "_standard_gap_health", lambda *_: {})
    write_health = MagicMock()
    monkeypatch.setattr(inference, "_write_health", write_health)

    published: list[tuple[str, str | None]] = []

    def publish(*, task_id, **_kwargs):
        published.append((task_id, redis.get(lock)))
        if publish_state != "failed":
            assert inference._claim_reserved_task(
                redis, lock=lock, heartbeat=heartbeat, task_id=task_id,
            )
        if publish_state == "finished":
            inference._release_owned_task(redis, lock=lock, heartbeat=heartbeat, task_id=task_id)
        if publish_state in {"failed", "claimed_then_failed"}:
            raise RuntimeError("broker response failed")

    monkeypatch.setattr(inference.run_camera_inference, "apply_async", publish)

    inference.supervise_all.run()

    assert len(published) == 1
    task_id, reserved_id = published[0]
    assert task_id and task_id == reserved_id
    assert task_id != "superseded-task"
    assert write_health.call_args.kwargs["reservation_health"]["standard_reservations_repaired"] == int(
        lost_reservation
    )
    if publish_state in {"failed", "finished"}:
        assert lock not in redis.values
        assert heartbeat not in redis.values
    else:
        assert redis.get(lock) == redis.get(heartbeat) == task_id
        assert redis.ttls[lock] == inference.LOCK_TTL


@pytest.mark.parametrize(
    "condition",
    ["recent_completion", "young_reservation", "active_heartbeat", "active_ttl", "broker_backlog"],
)
def test_standard_repair_preserves_live_or_recent_reservations(condition):
    r = FakeRedis()
    camera_id = 84
    lock = inference.LOCK_KEY_FMT.format(camera_id=camera_id)
    heartbeat = inference.HB_KEY_FMT.format(camera_id=camera_id)
    sla = inference.STANDARD_GAP_SLA_SECONDS
    now = 10_000.0
    last_run = now - sla - 1
    r.values[lock] = "current-task"
    r.ttls[lock] = inference.PENDING_LOCK_TTL - sla
    if condition == "recent_completion":
        last_run = now - sla
    elif condition == "young_reservation":
        r.ttls[lock] += 1
    elif condition == "active_heartbeat":
        r.values[heartbeat] = "current-task"
    elif condition == "active_ttl":
        r.ttls[lock] = inference.LOCK_TTL
    else:
        queue = inference._inference_queue(camera_id)
        r.queue_depths[f"{queue}{inference.REDIS_PRIORITY_SEP}9"] = 1

    assert inference._repair_stalled_pending_reservations(
        r, [camera_id], {camera_id: last_run}, gap_sla_seconds=sla, now=now,
        require_empty_queue=True,
    ) == 0
    assert r.values[lock] == "current-task"


def test_standard_coverage_uses_newer_processed_frames_during_long_tasks():
    r = FakeLastRunRedis(
        {1: "10", 2: None, 3: "950", 4: "10"},
        samples={
            1: json.dumps({"window_n": 100, "ts": 960}),
            2: json.dumps({"window_n": 1, "ts": 955}),
            3: json.dumps({"window_n": 100, "ts": 920}),
        },
    )

    health = inference._standard_gap_health(r, [1, 2, 3, 4], now=1000)
    assert health["standard_camera_ids_overdue"] == [4]
    assert health["standard_cameras_never_started"] == 0
    assert health["standard_max_gap_seconds"] == 990.0

    health = inference._standard_gap_health(r, [1, 2, 3], now=1000)
    assert health["standard_cameras_overdue"] == 0
    assert health["standard_max_gap_seconds"] == 50.0


@pytest.mark.parametrize("sample", [
    b"\xff",
    "malformed",
    "[]",
    '{"window_n": 0, "ts": 999}',
    '{"window_n": true, "ts": 999}',
    '{"window_n": 100, "ts": 1001}',
    '{"window_n": 100, "ts": "NaN"}',
    '{"window_n": 100, "ts": "Infinity"}',
    '{"window_n": 100, "ts": 20}',
])
def test_standard_coverage_does_not_clear_gaps_with_invalid_or_stale_frames(sample):
    r = FakeLastRunRedis({1: "30"}, samples={1: sample})

    health = inference._standard_gap_health(r, [1], now=1000)

    assert health["standard_camera_ids_overdue"] == [1]
    assert health["standard_max_gap_seconds"] == 970.0


@pytest.mark.parametrize(("shutdown_flag", "exit_code"), [
    ("should_stop", 0),
    ("should_terminate", 1),
])
@pytest.mark.parametrize("jpeg", [None, b"invalid jpeg"])
def test_worker_shutdown_releases_camera_lease_after_current_poll(
    monkeypatch, shutdown_flag, exit_code, jpeg,
):
    from contextlib import nullcontext

    from celery.worker import state

    from app.ai import inference_worker
    from app.ai.env_config import HardwareEnv

    camera_id = 7
    owner = "inference-task"
    redis = FakeRedis()
    lock = inference.LOCK_KEY_FMT.format(camera_id=camera_id)
    heartbeat = inference.HB_KEY_FMT.format(camera_id=camera_id)
    redis.values[lock] = owner
    redis.ttls[lock] = inference.PENDING_LOCK_TTL
    monkeypatch.setattr(state, "should_stop", None)
    monkeypatch.setattr(state, "should_terminate", None)
    monkeypatch.setattr(inference, "_redis", lambda: redis)
    monkeypatch.setattr(inference_worker.redis, "from_url", lambda _url: redis)
    monkeypatch.setattr(inference_worker, "_make_tracker", lambda _id: object())
    monkeypatch.setattr(inference_worker, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(
        inference_worker, "_load_camera_state", lambda *_args: (object(), [], {}, ""),
    )
    monkeypatch.setattr(inference_worker.settings, "reid_enabled", False)
    monkeypatch.setattr(
        HardwareEnv, "detect", lambda: SimpleNamespace(backend="cpu", export_format="pytorch"),
    )
    monkeypatch.setattr(
        inference_worker, "time",
        SimpleNamespace(time=lambda: 1.0, sleep=lambda _: None, perf_counter=lambda: 0.0),
    )

    # A second poll is a regression. Fail promptly on the unpatched loop.
    def poll_frame(_camera_id, *, prefer_overlay):
        assert not prefer_overlay
        if getattr(state, shutdown_flag) is not None:
            raise AssertionError("read another frame after worker shutdown")
        assert redis.get(lock) == redis.get(heartbeat) == owner
        setattr(state, shutdown_flag, exit_code)
        return jpeg

    poll = MagicMock(side_effect=poll_frame)
    monkeypatch.setattr(
        inference_worker, "FrameBuffer",
        lambda: SimpleNamespace(latest_jpeg=poll, health=lambda _id: {"last_frame_at": 1.0}),
    )

    task = inference.run_camera_inference
    task.push_request(id=owner)
    try:
        task.run(camera_id, max_seconds=3600)
    finally:
        task.pop_request()

    poll.assert_called_once_with(camera_id, prefer_overlay=False)
    assert lock not in redis.values
    assert heartbeat not in redis.values
    assert inference.LAST_RUN_KEY_FMT.format(camera_id=camera_id) not in redis.values


def test_compose_delivers_container_stop_signal_to_inference_worker():
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    section = compose.split("\n  worker-inference:\n", 1)[1].split("\n  worker-", 1)[0]
    assert '- "exec celery -A app.tasks.celery_app worker ' in section


def test_camera_task_records_completed_frame_time_before_idle_tail(monkeypatch):
    from app.ai import inference_worker

    camera_id = 7
    owner = "test-task"
    redis = FakeRedis()
    lock = inference.LOCK_KEY_FMT.format(camera_id=camera_id)
    redis.values[lock] = owner
    redis.ttls[lock] = inference.PENDING_LOCK_TTL
    monkeypatch.setattr(inference, "_redis", lambda: redis)
    monkeypatch.setattr(inference, "time", SimpleNamespace(time=lambda: 1_100.0))
    monkeypatch.setattr(inference_worker, "run_for_camera", lambda *_args, **_kwargs: 50.25)

    task = inference.run_camera_inference
    task.push_request(id=owner)
    try:
        task.run(camera_id, max_seconds=3600)
    finally:
        task.pop_request()

    completed = redis.get(inference.LAST_RUN_KEY_FMT.format(camera_id=camera_id))
    assert float(completed) == 50.25
    health = inference._standard_gap_health(
        FakeLastRunRedis({camera_id: completed}), [camera_id], now=1_100.0,
    )
    assert health["standard_camera_ids_overdue"] == [camera_id]


def test_inference_perf_tail_retains_last_completed_frame_time(monkeypatch):
    from contextlib import nullcontext

    from app import database
    from app.ai import perf_tracker

    clock = SimpleNamespace(now=50.25)
    redis = MagicMock()
    monkeypatch.setattr(perf_tracker, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(MagicMock()))
    perf = perf_tracker.PerfTracker(7, redis_client=redis)
    perf.record("total", 25.0)
    perf.record_frame()
    clock.now = 1_100.0
    perf.emit_final()

    payload = json.loads(redis.set.call_args.args[1])
    assert payload["ts"] == 50.25
    assert payload["window_n"] == 1
