import json

from app.tasks.agents import _heartbeat_age_seconds


def test_supervisor_json_heartbeat_uses_last_run_timestamp():
    raw = json.dumps({"last_run_ts": 1_000, "cameras_total": 101})
    assert _heartbeat_age_seconds(raw, now_ts=1_045) == 45


def test_legacy_supervisor_heartbeat_remains_supported():
    assert _heartbeat_age_seconds("1000", now_ts=1_045) == 45
    assert _heartbeat_age_seconds(None, now_ts=1_045) is None
