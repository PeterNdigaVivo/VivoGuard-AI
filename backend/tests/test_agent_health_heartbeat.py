import json
import time
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from app.tasks import alerting
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
    assert routes["inference.health_watchdog"]["queue"] == "beat"


@pytest.mark.parametrize(
    ("has_anchor", "suppressed", "sends_notification"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_pipeline_outage_checks_policy_before_commit(
    monkeypatch, has_anchor, suppressed, sends_notification,
):
    redis = Mock()
    sent_key = "vg:inference_pipeline_alert:sent"
    redis.get.side_effect = {
        "vg:inference:health": json.dumps({
            "last_run_ts": time.time() - alerting.INFERENCE_STALL_THRESHOLD_S - 60,
            "cameras_total": 3,
        }),
        sent_key: None,
    }.get
    monkeypatch.setattr(alerting, "_redis", lambda: redis)

    session = Mock()
    session.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        (1,) if has_anchor else None
    )

    class Event:
        @property
        def extra(self):
            assert not session.commit.called, "committed event policy has expired"
            return {"quality_control": {"notification_suppressed": suppressed}}

    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(session))
    create_alert = Mock(return_value=Event())
    monkeypatch.setattr(alerting, "_create_info_alert", create_alert)
    monkeypatch.setattr(alerting, "_dashboard_recipients", lambda: ["ops"])
    send = Mock()
    monkeypatch.setattr(alerting, "_send_whatsapp", send)

    alerting.inference_pipeline_health_check.run()

    assert create_alert.called is has_anchor
    assert session.commit.called is has_anchor
    assert send.called is sends_notification
    if has_anchor:
        redis.set.assert_called_once_with(sent_key, "1")
    else:
        redis.set.assert_not_called()
