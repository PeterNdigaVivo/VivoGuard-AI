"""After-hours person alerts must fail OPEN.

If the "is this store closed?" check breaks, the intrusion alert is
still sent and the failure is logged — a real 2 a.m. break-in must
never be dropped because of a config or cache problem.
"""
import logging
from types import SimpleNamespace

import pytest

import app.ai.inference_worker as iw
import app.utils.business_hours as bh


class _FakeRedis:
    """Minimal in-memory stand-in for the two calls these helpers make."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.data: dict[str, str] = {}

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("redis.from_url", lambda *a, **k: r)
    return r


@pytest.fixture(autouse=True)
def _reset_log_throttle():
    iw._fail_open_last_logged.clear()
    yield
    iw._fail_open_last_logged.clear()


def _ev(zone_id=None):
    return SimpleNamespace(zone_id=zone_id, detection_type="person")


STORE = SimpleNamespace(id=7)


def _hours(monkeypatch, *, result=None, exc=None):
    def fake(store, **_kw):
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(bh, "is_after_hours_with_grace", fake)


def test_hours_check_failure_alerts_and_logs(monkeypatch, fake_redis, caplog):
    _hours(monkeypatch, exc=ValueError("bad hours json"))
    with caplog.at_level(logging.ERROR, logger=iw.log.name):
        assert iw._person_alert_warranted(_ev(), [], STORE) is True
    assert "store=7" in caplog.text
    assert "bad hours json" in caplog.text


def test_hours_check_failure_log_is_throttled(monkeypatch, fake_redis, caplog):
    _hours(monkeypatch, exc=ValueError("bad hours json"))
    with caplog.at_level(logging.ERROR, logger=iw.log.name):
        for _ in range(5):
            assert iw._person_alert_warranted(_ev(), [], STORE) is True
    assert caplog.text.count("business-hours check failed") == 1


def test_after_hours_alerts_and_trading_hours_does_not(monkeypatch, fake_redis):
    _hours(monkeypatch, result=True)
    assert iw._person_alert_warranted(_ev(), [], STORE) is True
    _hours(monkeypatch, result=False)
    assert iw._person_alert_warranted(_ev(), [], STORE) is False


def test_restricted_zone_alerts_even_in_trading_hours(monkeypatch, fake_redis):
    _hours(monkeypatch, result=False)
    zones = [{"id": 3, "detection_types_json": ["stockroom"]}]
    assert iw._person_alert_warranted(_ev(zone_id=3), zones, STORE) is True


def test_no_store_context_is_not_an_intrusion(fake_redis):
    assert iw._person_alert_warranted(_ev(), [], None) is False


def test_unreadable_close_marker_still_checks_hours(monkeypatch, caplog):
    monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis(fail=True))
    _hours(monkeypatch, result=True)
    with caplog.at_level(logging.WARNING, logger=iw.log.name):
        assert iw._person_alert_warranted(_ev(), [], STORE) is True
    assert "actual-close marker unreadable" in caplog.text


def test_suppress_check_fails_open_on_unexpected_error(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(iw, "_person_alert_warranted", boom)
    with caplog.at_level(logging.ERROR, logger=iw.log.name):
        assert iw._person_alert_suppressed(_ev(), [], STORE, camera_id=11) is False
    assert "cam=11" in caplog.text


def test_suppress_check_dedupes_within_window(monkeypatch, fake_redis):
    _hours(monkeypatch, result=True)
    assert iw._person_alert_suppressed(_ev(), [], STORE, camera_id=11) is False
    # Second sighting inside the dedupe window: stored, not re-alerted.
    assert iw._person_alert_suppressed(_ev(), [], STORE, camera_id=11) is True


def test_suppress_check_alerts_when_redis_down(monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis(fail=True))
    _hours(monkeypatch, result=True)
    assert iw._person_alert_suppressed(_ev(), [], STORE, camera_id=11) is False


def test_trading_hours_traffic_is_suppressed(monkeypatch, fake_redis):
    _hours(monkeypatch, result=False)
    assert iw._person_alert_suppressed(_ev(), [], STORE, camera_id=11) is True
