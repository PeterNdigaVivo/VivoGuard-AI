"""Integration tests for the live_activity_sentinel beat task.

Drives the REAL task body end-to-end with an in-memory fake Redis, a
fake DB session, and captured _create_info_alert calls — verifying the
enable gate, threshold firing, SET-NX dedupe, after-hours suppression,
and the dead_scene kill-switch.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("celery")
pytest.importorskip("redis")

import app.tasks.activity_sentinel as sentinel            # noqa: E402
from app.models import Camera, DetectionConfig, Store     # noqa: E402


# ── fakes ──────────────────────────────────────────────────────────────────
class FakePipeline:
    def __init__(self, store: dict):
        self.store = store
        self.ops: list[tuple] = []

    def rpush(self, key, val):  self.ops.append(("rpush", key, val))
    def ltrim(self, key, a, b): self.ops.append(("ltrim", key, a, b))
    def expire(self, key, ttl): self.ops.append(("expire", key, ttl))
    def lrange(self, key, a, b): self.ops.append(("lrange", key, a, b))
    def exists(self, key):       self.ops.append(("exists", key))

    def execute(self):
        out = []
        for op in self.ops:
            kind, key = op[0], op[1]
            if kind == "rpush":
                self.store.setdefault(key, []).append(op[2])
                out.append(len(self.store[key]))
            elif kind == "ltrim":
                lst = self.store.get(key, [])
                a, b = op[2], op[3]
                self.store[key] = lst[a:] if b == -1 else lst[a:b + 1]
                out.append(True)
            elif kind == "expire":
                out.append(True)
            elif kind == "lrange":
                out.append(list(self.store.get(key, [])))
            elif kind == "exists":
                out.append(1 if key in self.store else 0)
        self.ops = []
        return out


class FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def pipeline(self, transaction=False):
        return FakePipeline(self.store)


class _Q:
    """Chainable query stub — filter/group_by are no-ops over canned rows."""
    def __init__(self, rows): self.rows = rows
    def filter(self, *a):   return self
    def group_by(self, *a): return self
    def all(self):          return self.rows
    def __iter__(self):     return iter(self.rows)


class FakeDB:
    """Duck-typed Session covering exactly the queries the task runs."""
    def __init__(self, cams, stores, overrides, staff_rows=None):
        self._cams, self._stores, self._overrides = cams, stores, overrides
        self.staff_rows = staff_rows if staff_rows is not None else []
        self.committed = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def commit(self):   self.committed += 1
    def rollback(self): pass

    def query(self, *args):
        from app.models import StaffTrack
        if args and args[0] is Camera.id:
            rows = self._cams
        elif args and args[0] is Store:
            rows = self._stores
        elif args and args[0] is DetectionConfig:
            rows = self._overrides
        elif args and args[0] is StaffTrack.store_id:
            rows = self.staff_rows
        else:                                       # pragma: no cover
            raise AssertionError(f"unexpected query args: {args}")
        return _Q(rows)


# ── fixture wiring ─────────────────────────────────────────────────────────
@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    """Two cameras in store 10 (open by default), sentinel enabled,
    surge threshold 12 / sustain 3, fresh activity blobs of 15 people."""
    r = FakeRedis()
    now = time.time()
    for cid in (1, 2):
        r.store[f"vg:activity:{cid}"] = json.dumps(
            {"camera_id": cid, "people": 15, "score": 15.0, "ts": now})

    cams = [(1, 10, "Cam A"), (2, 10, "Cam B")]
    stores = [SimpleNamespace(id=10, name="Vivo Test")]
    db = FakeDB(cams, stores, overrides=[], staff_rows=[(10, 1)])

    fired: list[dict] = []

    def _capture(dbs, *, camera_id, zone_id, store_id, detection_type,
                 cls, extra, capture_snapshot=True):
        fired.append({"camera_id": camera_id, "store_id": store_id,
                      "detection_type": detection_type, "cls": cls,
                      "extra": extra})

    import app.database as app_db
    import app.tasks.alerting as alerting
    import app.utils.business_hours as bh
    monkeypatch.setattr(sentinel, "_redis", lambda: r)
    monkeypatch.setattr(app_db, "SessionLocal", lambda: db)
    monkeypatch.setattr(alerting, "_create_info_alert", _capture)
    monkeypatch.setattr(bh, "is_store_open", lambda s: True)

    from app.config import settings
    for k, v in [("activity_sentinel_enabled", True),
                 ("activity_surge_people", 12),
                 ("activity_surge_sustain_samples", 3),
                 ("activity_store_surge_people", 30),
                 ("activity_dead_scene_minutes", 0),
                 # presence off by default in this fixture so the surge
                 # volume expectations stay exact; the presence test
                 # flips it on explicitly.
                 ("activity_presence_enabled", False),
                 ("activity_presence_threshold", 1),
                 ("activity_presence_sustain_samples", 2)]:
        monkeypatch.setattr(settings, k, v, raising=False)

    return SimpleNamespace(redis=r, db=db, fired=fired,
                           monkeypatch=monkeypatch, bh=bh)


def _run(n: int = 1) -> None:
    for _ in range(n):
        sentinel.live_activity_sentinel()


# ── tests ──────────────────────────────────────────────────────────────────
def test_disabled_flag_is_a_total_noop(env) -> None:
    from app.config import settings
    env.monkeypatch.setattr(settings, "activity_sentinel_enabled", False,
                            raising=False)
    _run(3)
    assert env.fired == []
    # No windows were even built — the gate short-circuits before Redis.
    assert not any(k.startswith("vg:activity:hist:") for k in env.redis.store)


def test_fires_after_sustain_and_maps_rules(env) -> None:
    _run(2)
    assert env.fired == []                    # 2 samples < sustain 3
    _run(1)
    rules = sorted(f["cls"] for f in env.fired)
    assert rules == ["occupancy_surge", "occupancy_surge", "store_surge"]
    for f in env.fired:
        assert f["detection_type"] == "live_activity"
        assert f["extra"]["rule"] == f["cls"]
        assert f["extra"]["source"] == "live_activity_sentinel"
    store_t = next(f for f in env.fired if f["cls"] == "store_surge")
    assert store_t["store_id"] == 10
    assert store_t["extra"]["people_count"] == 30
    # Breakdown rides on every people_count rule: 1 staff, rest customers.
    assert store_t["extra"]["staff_count"] == 1
    assert store_t["extra"]["customer_count"] == 29
    assert env.db.committed >= 1


def test_dedupe_blocks_repeat_alerts_within_ttl(env) -> None:
    _run(3)
    n_first = len(env.fired)
    assert n_first == 3
    _run(2)                                   # still over threshold
    assert len(env.fired) == n_first          # SET-NX buckets held


def test_after_hours_fires_and_intrusion_suppresses(env) -> None:
    env.monkeypatch.setattr(env.bh, "is_store_open", lambda s: False)
    _run(1)                                   # 1 sample: surges can't fire
    assert [f["cls"] for f in env.fired] == ["after_hours_activity"]
    assert env.fired[0]["extra"]["severity"] == "URGENT"

    # Reset dedupe + windows, arm the intrusion session key → suppressed.
    env.redis.store = {k: v for k, v in env.redis.store.items()
                       if k.startswith("vg:activity:") and "hist" not in k}
    env.fired.clear()
    env.redis.store["vg:afterhours:open:10"] = "1"
    _run(1)
    assert env.fired == []


def test_dead_scene_off_at_zero_minutes_even_with_flat_zero(env) -> None:
    now = time.time()
    for cid in (1, 2):
        env.redis.store[f"vg:activity:{cid}"] = json.dumps(
            {"camera_id": cid, "people": 0, "score": 0.0, "ts": now})
        env.redis.store[f"vg:frame:{cid}"] = "jpeg"
        # Pre-age the window so the span requirement would pass if enabled.
        env.redis.store[f"vg:activity:hist:{cid}"] = [
            json.dumps({"people": 0, "score": 0.0, "ts": now - 60 * i})
            for i in range(9, -1, -1)]
    _run(1)
    assert all(f["cls"] != "dead_scene" for f in env.fired)


def test_activity_presence_end_to_end_info_alert(env) -> None:
    from app.config import settings
    env.monkeypatch.setattr(settings, "activity_presence_enabled", True,
                            raising=False)
    # Low count (2 people) — below every surge threshold.
    now = time.time()
    for cid in (1, 2):
        env.redis.store[f"vg:activity:{cid}"] = json.dumps(
            {"camera_id": cid, "people": 2, "score": 2.0, "ts": now})
    _run(1)
    assert env.fired == []                    # 1 sample < sustain 2
    _run(1)
    presence = [f for f in env.fired if f["cls"] == "activity_presence"]
    assert sorted(f["camera_id"] for f in presence) == [1, 2]
    for f in presence:
        assert f["detection_type"] == "live_activity"
        assert f["extra"]["severity"] == "INFO"
        assert "Activity detected at Cam" in f["extra"]["message"]
        assert "2 people present" in f["extra"]["message"]
        # Staff/customer breakdown from staff_tracks (fixture: 1 staff
        # active at store 10) — customers = people - staff.
        assert f["extra"]["staff_count"] == 1
        assert f["extra"]["customer_count"] == 1
        assert f["extra"]["breakdown_source"] == "staff_tracks_store_15min"
    # Dedupe: still active next tick → no new alerts.
    n = len(env.fired)
    _run(1)
    assert len(env.fired) == n


def test_stale_activity_blobs_are_ignored(env) -> None:
    stale = time.time() - 3600
    for cid in (1, 2):
        env.redis.store[f"vg:activity:{cid}"] = json.dumps(
            {"camera_id": cid, "people": 50, "score": 50.0, "ts": stale})
    _run(3)
    assert env.fired == []                    # nothing fresh → no windows
