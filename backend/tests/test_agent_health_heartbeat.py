import json
import time
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from app.tasks import agents, alerting
from app.tasks.agents import _heartbeat_age_seconds
from app.tasks.celery_app import celery_app


def test_supervisor_json_heartbeat_uses_last_run_timestamp():
    raw = json.dumps({"last_run_ts": 1_000, "cameras_total": 101})
    assert _heartbeat_age_seconds(raw, now_ts=1_045) == 45


def test_legacy_supervisor_heartbeat_remains_supported():
    assert _heartbeat_age_seconds("1000", now_ts=1_045) == 45
    assert _heartbeat_age_seconds(b'{"ts":1000}', now_ts=1_045) == 45
    assert _heartbeat_age_seconds(None, now_ts=1_045) is None


@pytest.mark.parametrize("raw", [b"broken", b"\xff", b"{}", b"NaN", b"Infinity", b"true", b"0", b"1100"])
def test_invalid_heartbeat_is_unavailable(raw):
    assert _heartbeat_age_seconds(raw, now_ts=1_045) is None


@pytest.mark.parametrize("read_failure", [False, True])
def test_backend_health_warns_when_inference_heartbeat_is_unverifiable(monkeypatch, read_failure):
    redis = Mock()
    if read_failure:
        redis.get.side_effect = RuntimeError("private diagnostic")
    else:
        redis.get.return_value = b"private diagnostic"
    monkeypatch.setattr(agents, "_redis", lambda: redis)
    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(Mock()))

    report = agents._run_backend_health()

    assert report["status"] == "warning"
    assert report["findings"]["inference_health_age_s"] is None
    assert report["gaps"] == [{"inference": "health breadcrumb unavailable"}]
    assert "private diagnostic" not in json.dumps(report)


def test_inference_supervision_uses_non_blocking_beat_queue():
    routes = celery_app.conf.task_routes
    assert routes["inference.supervise_all"]["queue"] == "beat"
    assert routes["alerting.inference_pipeline_health_check"]["queue"] == "beat"
    assert routes["inference.health_watchdog"]["queue"] == "beat"


@pytest.mark.parametrize(
    ("checks", "streaming", "dark_cameras", "unverified"),
    [
        ([1, 1, 1], 3, [], 0),
        ([1, 0, RuntimeError("private backend diagnostic")], 1, [2], 1),
        ([RuntimeError("private backend diagnostic")] * 3, 0, [], 3),
    ],
)
def test_streamer_health_distinguishes_missing_frames_from_failed_checks(
    monkeypatch, checks, streaming, dark_cameras, unverified,
):
    redis = Mock()
    redis.exists.side_effect = checks
    db = Mock()
    db.execute.return_value.all.return_value = [(1,), (2,), (3,)]
    monkeypatch.setattr(agents, "_redis", lambda: redis)
    monkeypatch.setattr("app.database.SessionLocal", lambda: nullcontext(db))

    report = agents._run_streamer()

    assert report["findings"] == {
        "ai_enabled_cameras": 3, "streaming": streaming, "dark": len(dark_cameras),
        "frame_checks_unavailable": unverified,
    }
    gaps = []
    if dark_cameras:
        gaps.append({"dark_cameras": dark_cameras})
    if unverified:
        gaps.append({"frame_checks_unavailable": unverified})
    assert report["gaps"] == (gaps or None)
    assert report["status"] == ("warning" if gaps else "ok")


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
