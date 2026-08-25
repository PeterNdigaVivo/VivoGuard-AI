from datetime import datetime, time, timezone
from types import SimpleNamespace

from app.ai.detectors import shop_state
from app.tasks import alerting


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, cameras):
        self.cameras = cameras
        self.commits = 0

    def query(self, _model):
        return _Query(self.cameras)

    def commit(self):
        self.commits += 1


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.keys = []

    def exists(self, key):
        self.keys.append(key)
        return self

    def execute(self):
        return [int(key in self.redis.fresh_keys) for key in self.keys]


class _Redis:
    def __init__(self, fresh_camera_ids=()):
        self.fresh_keys = {f"vg:frame:{camera_id}" for camera_id in fresh_camera_ids}
        self.values = {}

    def pipeline(self, transaction=False):
        return _Pipeline(self)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.values[key] = value


def _prepare(monkeypatch, fresh_camera_ids):
    store = SimpleNamespace(id=5, name="Vivo Moi Avenue")
    db = _Db([SimpleNamespace(id=36), SimpleNamespace(id=37)])
    redis = _Redis(fresh_camera_ids)
    created = []

    monkeypatch.setattr(alerting, "_entrance_cam_ids_for_store",
                        lambda _db, _store_id: [36, 37])
    monkeypatch.setattr(alerting, "_store_eat_now",
                        lambda _store: datetime(2026, 8, 25, 10, 0,
                                                tzinfo=timezone.utc))
    monkeypatch.setattr(shop_state, "store_opened_today",
                        lambda _store_id, _day: None)
    monkeypatch.setattr(alerting, "_occupancy_fallback_for_store",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(alerting, "_dashboard_recipients", lambda: [])

    def create(_db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(alerting, "_create_info_alert", create)

    def cfg(_zone):
        return {"not_open_cutoff_t": time(9, 30)}

    return store, db, redis, created, cfg


def test_not_opened_urgent_is_suppressed_without_fresh_entrance_coverage(monkeypatch):
    store, db, redis, created, cfg = _prepare(monkeypatch, ())

    alerting._shop_not_opened_for_store(db, redis, store, cfg)

    assert created == []
    assert db.commits == 0


def test_not_opened_alert_anchors_to_a_fresh_entrance_camera(monkeypatch):
    store, db, redis, created, cfg = _prepare(monkeypatch, (37,))

    alerting._shop_not_opened_for_store(db, redis, store, cfg)

    assert len(created) == 1
    assert created[0]["camera_id"] == 37
    assert db.commits == 1
