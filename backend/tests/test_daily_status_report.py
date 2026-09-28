"""The daily "VivoGuard Status Report" email: send window, once-a-day
dedupe, missing-SMTP skip, and retry-on-failure."""
from datetime import datetime

import pytest

import app.tasks.system_health_report as rpt


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True


class _FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if _FakeSMTP.fail:
            raise OSError("smtp down")
        _FakeSMTP.sent.append(msg)


class _NullSession:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


_Y = {"label": "Sunday", "visitors": None, "alerts": None, "peak": None,
      "feedback": None, "degraded": False}


class _Retry(Exception):
    pass


def _at(hour, minute):
    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, hour, minute, tzinfo=rpt.EAT)
    return _FixedDT


@pytest.fixture
def env(monkeypatch):
    r = _FakeRedis()
    _FakeSMTP.sent = []
    _FakeSMTP.fail = False
    retries: list = []

    def fake_retry(exc=None, countdown=None):
        retries.append(countdown)
        raise _Retry()

    monkeypatch.setattr("redis.from_url", lambda *a, **k: r)
    monkeypatch.setattr(rpt.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(rpt.settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(rpt.settings, "smtp_user", "")
    monkeypatch.setattr("app.database.SessionLocal", _NullSession)
    monkeypatch.setattr("app.utils.system_health.collect_system_health",
                        lambda db: {})
    monkeypatch.setattr(rpt, "_collect_yesterday", lambda db: dict(_Y))
    monkeypatch.setattr(rpt, "_friendly_health",
                        lambda snap: ("All systems OK", [("✅", "fine")]))
    monkeypatch.setattr(rpt.daily_status_report, "retry", fake_retry)
    monkeypatch.setattr(rpt, "datetime", _at(11, 35))
    return {"redis": r, "retries": retries, "mp": monkeypatch}


def _run(force=False):
    rpt.daily_status_report.run(force=force)


def test_sends_inside_window_to_system_admins_and_marks_day(env):
    _run()
    assert len(_FakeSMTP.sent) == 1
    msg = _FakeSMTP.sent[0]
    assert msg["Subject"] == "VivoGuard Status Report — 2026-09-28"
    assert set(msg["To"].split(", ")) == set(rpt.SYSTEM_ADMIN_EMAILS)
    assert env["redis"].get(rpt._sent_key("2026-09-28")) == "1"


def test_outside_window_does_nothing(env):
    env["mp"].setattr(rpt, "datetime", _at(11, 29))
    _run()
    env["mp"].setattr(rpt, "datetime", _at(11, 45))
    _run()
    assert _FakeSMTP.sent == []


def test_force_skips_clock_gate(env):
    env["mp"].setattr(rpt, "datetime", _at(8, 0))
    _run(force=True)
    assert len(_FakeSMTP.sent) == 1


def test_only_once_per_day(env):
    _run()
    _run(force=True)       # even a forced send respects the sent marker
    assert len(_FakeSMTP.sent) == 1


def test_missing_smtp_host_skips(env):
    env["mp"].setattr(rpt.settings, "smtp_host", "")
    _run()
    assert _FakeSMTP.sent == []


def test_smtp_failure_retries_and_does_not_mark_day(env):
    _FakeSMTP.fail = True
    with pytest.raises(_Retry):
        _run()
    assert env["retries"] == [rpt._RETRY_COUNTDOWN_S]
    assert env["redis"].get(rpt._sent_key("2026-09-28")) is None


def test_schedule_and_routing_are_registered():
    from app.tasks.celery_app import celery_app
    entry = celery_app.conf.beat_schedule["vivoguard-status-report-every-5min"]
    assert entry["task"] == "system.daily_status_report"
    assert entry["schedule"].total_seconds() == 300
    assert celery_app.conf.task_routes["system.daily_status_report"] == {"queue": "beat"}
