from app.tasks.inference_watchdog import _signature, inference_health_problems


def test_watchdog_identifies_only_stalled_shards():
    authoritative = {
        "inference_shards": {
            "inference.0": {
                "cameras": 30, "active": 8, "queue_depth": 22,
            },
            "inference.1": {
                "cameras": 28, "active": 0, "queue_depth": 28,
            },
        },
    }

    assert inference_health_problems(
        authoritative,
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    ) == [{
        "code": "shard_not_consuming",
        "queue": "inference.1",
        "queue_depth": 28,
        "assigned_cameras": 28,
    }]


def test_watchdog_does_not_require_disabled_shadow():
    assert inference_health_problems(
        {"inference_shards": {}},
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    ) == []


def test_watchdog_reports_expected_missing_or_unsafe_shadow():
    assert inference_health_problems(
        {"inference_shards": {}},
        None,
        shadow_expected=True,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    ) == [{"code": "batch_shadow_missing"}]

    problems = inference_health_problems(
        {"inference_shards": {}},
        {
            "last_run_ts": 700,
            "authoritative": True,
            "errors": 2,
            "max_camera_schedule_wait_seconds": 3,
        },
        shadow_expected=True,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    )
    assert [problem["code"] for problem in problems] == [
        "batch_shadow_stale", "batch_shadow_unsafe_mode",
        "batch_shadow_errors", "batch_shadow_schedule_wait",
    ]


def test_watchdog_signature_ignores_volatile_depth_and_age():
    assert _signature([{
        "code": "shard_not_consuming", "queue": "inference.1",
        "queue_depth": 10,
    }]) == _signature([{
        "code": "shard_not_consuming", "queue": "inference.1",
        "queue_depth": 20,
    }])
    assert _signature([{
        "code": "batch_shadow_stale", "age_seconds": 130,
    }]) == _signature([{
        "code": "batch_shadow_stale", "age_seconds": 190,
    }])
