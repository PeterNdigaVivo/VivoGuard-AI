import json

from app.tasks.agents import _heartbeat_age_seconds
from app.tasks.celery_app import celery_app


def test_supervisor_json_heartbeat_uses_last_run_timestamp():
    raw = json.dumps({"last_run_ts": 1_000, "cameras_total": 101})
    assert _heartbeat_age_seconds(raw, now_ts=1_045) == 45


def test_legacy_supervisor_heartbeat_remains_supported():
    assert _heartbeat_age_seconds("1000", now_ts=1_045) == 45
    assert _heartbeat_age_seconds(None, now_ts=1_045) is None


def test_inference_supervision_uses_non_blocking_beat_queue():
    routes = celery_app.conf.task_routes
    assert routes["inference.supervise_all"]["queue"] == "beat"
    assert routes["alerting.inference_pipeline_health_check"]["queue"] == "beat"
