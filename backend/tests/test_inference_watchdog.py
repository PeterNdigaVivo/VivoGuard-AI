import json

from app.tasks import inference_watchdog
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


def test_watchdog_reports_critical_camera_gap_without_gpu_shadow():
    problems = inference_health_problems(
        {
            "inference_shards": {},
            "critical_cameras_overdue": 2,
            "critical_camera_ids_overdue": [17, 23],
            "critical_max_gap_seconds": 421.0,
            "critical_gap_sla_seconds": 300,
        },
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    )

    assert problems == [{
        "code": "critical_camera_gap_sla",
        "overdue_cameras": 2,
        "camera_ids": [17, 23],
        "max_gap_seconds": 421.0,
        "sla_seconds": 300,
    }]


def test_watchdog_reports_standard_camera_gap_without_gpu_shadow():
    problems = inference_health_problems(
        {
            "inference_shards": {},
            "standard_cameras_overdue": 3,
            "standard_camera_ids_overdue": [31, 44, 52],
            "standard_max_gap_seconds": 1081.0,
            "standard_gap_sla_seconds": 900,
        },
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    )

    assert problems == [{
        "code": "standard_camera_gap_sla",
        "overdue_cameras": 3,
        "camera_ids": [31, 44, 52],
        "max_gap_seconds": 1081.0,
        "sla_seconds": 900,
    }]


def test_watchdog_warns_before_standard_camera_gap_breaches_sla():
    problems = inference_health_problems(
        {
            "inference_shards": {},
            "standard_cameras_overdue": 0,
            "standard_max_gap_seconds": 725.0,
            "standard_gap_sla_seconds": 900,
        },
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
        capacity_headroom_percent=80,
    )

    assert problems == [{
        "code": "standard_camera_gap_headroom_low",
        "max_gap_seconds": 725.0,
        "sla_seconds": 900,
        "headroom_percent": 80,
        "remaining_seconds": 175.0,
    }]


def test_watchdog_does_not_duplicate_headroom_when_gap_is_overdue():
    problems = inference_health_problems(
        {
            "inference_shards": {},
            "standard_cameras_overdue": 1,
            "standard_camera_ids_overdue": [31],
            "standard_max_gap_seconds": 910.0,
            "standard_gap_sla_seconds": 900,
        },
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
        capacity_headroom_percent=80,
    )

    assert [problem["code"] for problem in problems] == [
        "standard_camera_gap_sla",
    ]


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


def test_watchdog_does_not_rearm_during_same_changing_outage(monkeypatch):
    authoritative = {
        "inference_shards": {
            "inference": {"cameras": 10, "active": 0, "queue_depth": 4},
        },
    }

    class FakeRedis:
        def __init__(self):
            self.values = {
                inference_watchdog.AUTHORITATIVE_KEY: json.dumps(authoritative),
                inference_watchdog.STATE_KEY: json.dumps({
                    "signature": "previous-problem-composition",
                    "first_seen_ts": 123.0,
                }),
                inference_watchdog.SENT_KEY: "previous-alert",
            }

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, **_kwargs):
            self.values[key] = value

        def delete(self, *keys):
            for key in keys:
                self.values.pop(key, None)

    fake_redis = FakeRedis()
    monkeypatch.setattr(inference_watchdog.redis, "from_url", lambda *_a: fake_redis)

    inference_watchdog.inference_health_watchdog.run()

    state = json.loads(fake_redis.values[inference_watchdog.STATE_KEY])
    problems = inference_health_problems(
        authoritative,
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    )
    assert state["signature"] == _signature(problems)
    assert state["first_seen_ts"] == 123.0
    assert state["last_change_ts"] >= 123.0
    assert fake_redis.values[inference_watchdog.SENT_KEY] == "previous-alert"


def test_watchdog_checks_notification_policy_before_session_detaches(monkeypatch):
    authoritative = {
        "inference_shards": {
            "inference": {"cameras": 10, "active": 0, "queue_depth": 4},
        },
    }
    problems = inference_health_problems(
        authoritative,
        None,
        shadow_expected=False,
        now=1000,
        max_shadow_age_seconds=120,
        max_schedule_wait_seconds=2,
    )

    class FakeRedis:
        def __init__(self):
            self.values = {
                inference_watchdog.AUTHORITATIVE_KEY: json.dumps(authoritative),
                inference_watchdog.STATE_KEY: json.dumps({
                    "signature": _signature(problems),
                    "first_seen_ts": 1,
                }),
            }

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, **_kwargs):
            self.values[key] = value

        def delete(self, *keys):
            for key in keys:
                self.values.pop(key, None)

    class Event:
        attached = True

        @property
        def extra(self):
            if not self.attached:
                raise RuntimeError("detached event accessed")
            return {}

    event = Event()

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return (1,)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            event.attached = False

        def query(self, *_args):
            return Query()

        def commit(self):
            return None

    fake_redis = FakeRedis()
    sent = []
    monkeypatch.setattr(inference_watchdog.redis, "from_url", lambda *_a: fake_redis)

    import app.database
    import app.tasks.alerting

    monkeypatch.setattr(app.database, "SessionLocal", Session)
    monkeypatch.setattr(
        app.tasks.alerting, "_create_info_alert", lambda *_a, **_kw: event,
    )
    monkeypatch.setattr(app.tasks.alerting, "_dashboard_recipients", lambda: ["ops"])
    monkeypatch.setattr(
        app.tasks.alerting, "_send_whatsapp",
        lambda recipients, body: sent.append((recipients, body)),
    )

    inference_watchdog.inference_health_watchdog.run()

    assert sent and sent[0][0] == ["ops"]
    assert fake_redis.values[inference_watchdog.SENT_KEY] == _signature(problems)
