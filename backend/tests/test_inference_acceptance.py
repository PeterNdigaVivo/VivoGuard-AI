from app.services.inference_acceptance import (
    CapacityThresholds,
    evaluate_capacity_acceptance,
)


NOW = 10_000.0
THRESHOLDS = CapacityThresholds(
    max_health_age_seconds=120,
    min_uptime_seconds=7200,
    min_frames_per_camera=100,
    max_p95_per_frame_ms=400,
    max_schedule_wait_seconds=2,
)


def _authoritative():
    return {
        "last_run_ts": NOW - 5,
        "cameras_fresh": 58,
        "fresh_camera_ids": list(range(1, 59)),
    }


def _shadow():
    return {
        "last_run_ts": NOW - 2,
        "authoritative": False,
        "uptime_seconds": 7201,
        "errors": 0,
        "cameras_served": 58,
        "served_camera_ids": list(range(1, 59)),
        "frames_processed": 5800,
        "p95_per_frame_ms": 120,
        "max_camera_schedule_wait_seconds": 1.5,
    }


def test_capacity_pass_never_claims_accuracy_or_promotion():
    result = evaluate_capacity_acceptance(
        _authoritative(),
        _shadow(),
        now=NOW,
        baseline={"cameras_reporting": 58, "frames": 10000},
        thresholds=THRESHOLDS,
    )

    assert result["status"] == "capacity_ready"
    assert result["capacity_gate_passed"] is True
    assert result["accuracy_gate_evaluated"] is False
    assert result["promotion_ready"] is False


def test_capacity_fails_when_one_fresh_camera_is_starved():
    shadow = _shadow()
    shadow["cameras_served"] = 57
    shadow["served_camera_ids"] = list(range(1, 58))

    result = evaluate_capacity_acceptance(
        _authoritative(),
        shadow,
        now=NOW,
        baseline={"cameras_reporting": 58, "frames": 10000},
        thresholds=THRESHOLDS,
    )

    assert result["status"] == "failed"
    check = next(
        item for item in result["checks"]
        if item["name"] == "all_fresh_cameras_served"
    )
    assert check["passed"] is False


def test_capacity_is_pending_without_shadow_telemetry():
    result = evaluate_capacity_acceptance(
        _authoritative(),
        None,
        now=NOW,
        baseline={"cameras_reporting": 58, "frames": 10000},
        thresholds=THRESHOLDS,
    )

    assert result["status"] == "pending"
    assert result["capacity_gate_passed"] is False
