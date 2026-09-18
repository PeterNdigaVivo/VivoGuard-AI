from app.utils.system_health import _active_inference_count


def test_fresh_pipeline_is_authoritative_for_ai_active_count():
    assert _active_inference_count(
        legacy_active=0,
        pipeline={
            "last_run_ts": 990.0,
            "cameras_actively_inferencing": 71,
        },
        enabled_cameras=82,
        now=1_000.0,
    ) == 71


def test_ai_active_count_is_bounded_by_enabled_cameras():
    assert _active_inference_count(
        legacy_active=0,
        pipeline={
            "last_run_ts": 990.0,
            "cameras_actively_inferencing": 99,
        },
        enabled_cameras=82,
        now=1_000.0,
    ) == 82


def test_stale_or_malformed_pipeline_falls_back_to_legacy_heartbeats():
    assert _active_inference_count(
        legacy_active=4,
        pipeline={
            "last_run_ts": 800.0,
            "cameras_actively_inferencing": 71,
        },
        enabled_cameras=82,
        now=1_000.0,
    ) == 4
    assert _active_inference_count(
        legacy_active=3,
        pipeline={"last_run_ts": "not-a-time"},
        enabled_cameras=82,
        now=1_000.0,
    ) == 3
