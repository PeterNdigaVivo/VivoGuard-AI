import json

from app.ai.detectors.retail_checkout import CheckoutDwellDetector
from app.tasks.alerting import _checkout_session_is_fresh


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True


def test_checkout_session_requires_recent_observation():
    now = 1_000.0
    assert _checkout_session_is_fresh(
        {"entry_ts": 700.0, "last_seen_ts": 950.0}, now, 90)
    assert not _checkout_session_is_fresh(
        {"entry_ts": 700.0, "last_seen_ts": 800.0}, now, 90)
    # Legacy keys without a heartbeat fail closed once their entry is stale.
    assert not _checkout_session_is_fresh({"entry_ts": 700.0}, now, 90)
    assert not _checkout_session_is_fresh(
        {"entry_ts": 700.0, "last_seen_ts": "invalid"}, now, 90)


def test_checkout_heartbeat_preserves_alert_linkage():
    redis = _FakeRedis()
    detector = CheckoutDwellDetector()
    detector._publish_open(
        redis, 12, 10, 44, 700.0, 4,
        min_alert_seconds=180, last_seen_ts=900.0,
    )
    key = "vg:checkout_open:12:10:44"
    first = json.loads(redis.values[key])
    first["alert_id"] = 100269
    redis.values[key] = json.dumps(first)

    detector._publish_open(
        redis, 12, 10, 44, 700.0, 4,
        min_alert_seconds=300, last_seen_ts=995.0,
    )
    refreshed = json.loads(redis.values[key])
    assert refreshed["entry_ts"] == 700.0
    assert refreshed["last_seen_ts"] == 995.0
    assert refreshed["min_alert_seconds"] == 300
    assert refreshed["alert_id"] == 100269
