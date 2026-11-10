"""Durable heatmap (delta publishing) + counter-zone visit logging.

The core bug these pin: the HeatmapDetector's in-memory grids die with
every inference slice, and the old publish OVERWROTE the day/week keys
with that sub-slice view — long-window heatmaps reset every ~120s.
Delta publishing must make the durable totals survive a restart
(simulated here by constructing a brand-new detector against the same
fake Redis) and keep growing monotonically.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.ai.detectors.stateful import HeatmapDetector


# ---------------------------------------------------------------------
# Minimal fake Redis: just the commands the delta publisher uses.
# ---------------------------------------------------------------------

class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}

    # pipeline that applies commands immediately (ordering preserved)
    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def hincrby(self, key, field, amount):
        h = self.hashes.setdefault(key, {})
        h[field] = h.get(field, 0) + int(amount)
        return h[field]

    def hincrbyfloat(self, key, field, amount):
        h = self.hashes.setdefault(key, {})
        h[field] = h.get(field, 0.0) + float(amount)
        return h[field]

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expireat(self, key, when):
        return True

    def delete(self, key):
        self.hashes.pop(key, None)
        self.strings.pop(key, None)
        return 1

    def set(self, key, value, ex=None):
        self.strings[key] = value
        return True


class FakePipeline:
    def __init__(self, r: FakeRedis):
        self._r = r
        self._results: list = []

    def __getattr__(self, name):
        real = getattr(self._r, name)

        def _queued(*args, **kwargs):
            self._results.append(real(*args, **kwargs))
            return self

        return _queued

    def execute(self):
        out, self._results = self._results, []
        return out


@pytest.fixture()
def fake_redis(monkeypatch):
    r = FakeRedis()
    import redis as redis_mod
    monkeypatch.setattr(redis_mod, "from_url", lambda *a, **k: r)
    return r


def _bump_cells(det: HeatmapDetector, camera_id: int,
                cells: list[tuple[int, int]], n: int = 1) -> None:
    """Simulate `n` frames of traffic on the given cells."""
    for w in det.WINDOWS:
        det._ensure_grid(camera_id, w)
    for _ in range(n):
        for (gy, gx) in cells:
            for w in det.WINDOWS:
                det.grids[camera_id][w][gy][gx] += 1


def _day_sum(r: FakeRedis, camera_id: int) -> float:
    payload = json.loads(r.strings[f"vg:heatmap:day:{camera_id}"])
    return sum(sum(row) for row in payload["grid"])


def test_day_grid_survives_detector_restart(fake_redis):
    """The reported bug shape: a new slice process must ADD to the day
    grid, not replace it with its own sub-slice counts."""
    cam = 60
    det1 = HeatmapDetector()
    _bump_cells(det1, cam, [(5, 5), (6, 6)], n=10)   # 20 traffic counts
    det1._publish(cam)
    assert _day_sum(fake_redis, cam) == 20

    # Second publish from the SAME slice with no new activity: delta is
    # zero, totals unchanged (idempotent, no double counting).
    det1._publish(cam)
    assert _day_sum(fake_redis, cam) == 20

    # Slice restart: fresh instance, empty memory. Old behaviour wrote
    # 5 and the day total collapsed 20 -> 5. Delta publishing adds.
    det2 = HeatmapDetector()
    _bump_cells(det2, cam, [(5, 5)], n=5)
    det2._publish(cam)
    assert _day_sum(fake_redis, cam) == 25            # 20 + 5, no reset

    # And it keeps growing monotonically within the new slice.
    _bump_cells(det2, cam, [(1, 1)], n=3)
    det2._publish(cam)
    assert _day_sum(fake_redis, cam) == 28

    # Legacy un-suffixed key mirrors the durable day grid.
    legacy = json.loads(fake_redis.strings[f"vg:heatmap:{cam}"])
    assert sum(sum(row) for row in legacy["grid"]) == 28


def test_failed_publish_retries_same_delta(fake_redis, monkeypatch):
    """If the increment pipeline fails, the baseline must NOT advance:
    the same delta goes out on the next cycle, and nothing crashes."""
    cam = 61
    det = HeatmapDetector()
    _bump_cells(det, cam, [(2, 2)], n=4)

    real_pipeline = fake_redis.pipeline
    calls = {"n": 0}

    def exploding_pipeline(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:          # first pipeline = the increments
            raise ConnectionError("redis down")
        return real_pipeline(*a, **k)

    monkeypatch.setattr(fake_redis, "pipeline", exploding_pipeline)
    det._publish(cam)                 # must not raise
    assert f"vg:heatmap:day:{cam}" not in fake_redis.strings

    monkeypatch.setattr(fake_redis, "pipeline", real_pipeline)
    det._publish(cam)                 # retry carries the full delta
    assert _day_sum(fake_redis, cam) == 4


def test_live_boost_never_pollutes_accumulators(fake_redis):
    """The presentational live-activity boost must appear in the
    rendered engagement key but never compound into the durable
    accumulator hash."""
    cam = 62
    det = HeatmapDetector()
    _bump_cells(det, cam, [(0, 0)], n=1)
    det._live_cells[cam] = {(9, 9)}
    for _ in range(5):                # 5 publish cycles with the boost
        det._publish(cam)
    acc = fake_redis.hashes.get(
        det._acc_key("engagement", "day", cam), {})
    assert "9:9" not in acc           # accumulator untouched by boost
    rendered = json.loads(
        fake_redis.strings[f"vg:heatmap:engagement:day:{cam}"])
    assert rendered["grid"][9][9] > 0  # but the display shows it


def test_window_expiry_boundaries_are_eat():
    """day expires next midnight EAT; week next Monday 00:00 EAT."""
    from zoneinfo import ZoneInfo
    eat = ZoneInfo("Africa/Nairobi")
    now = datetime.now(eat)
    day_exp = datetime.fromtimestamp(
        HeatmapDetector._window_expiry_ts("day"), eat)
    assert day_exp.hour == 0 and day_exp.minute == 0
    assert day_exp.date() == (now + timedelta(days=1)).date()
    week_exp = datetime.fromtimestamp(
        HeatmapDetector._window_expiry_ts("week"), eat)
    assert week_exp.isoweekday() == 1          # Monday
    assert week_exp.hour == 0
    assert now < week_exp <= now + timedelta(days=7)


# ---------------------------------------------------------------------
# Counter-zone visit sessions (zone_visits)
# ---------------------------------------------------------------------

from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import sessionmaker                    # noqa: E402

from app.database import Base                              # noqa: E402
from app.models import Camera, Store, Zone, ZoneVisit      # noqa: E402

TABLES = [Store.__table__, Camera.__table__, Zone.__table__,
          ZoneVisit.__table__]


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=TABLES)
    s = sessionmaker(bind=eng)()
    store = Store(name="Vivo Moi Avenue", country="Kenya")
    s.add(store); s.flush()
    cam = Camera(name="Ch4", store_id=store.id, brand="dahua",
                 connection_type="rtsp", host="10.0.0.60")
    s.add(cam); s.flush()
    zone = Zone(camera_id=cam.id, name="Counter",
                polygon_coords_json=[[0, 0], [1, 0], [1, 1], [0, 1]],
                detection_types_json=["counter"])
    s.add(zone); s.commit()
    yield s, store, cam, zone
    s.close()


def _ctx(s, store, cam, zone, tracks):
    return SimpleNamespace(
        db=s, store_id=store.id, camera_id=cam.id,
        zones=[{"id": zone.id,
                "detection_types_json": ["counter"],
                "polygon_coords_json": zone.polygon_coords_json}],
        tracks=tracks)


def _person_track(track_id, bbox):
    return (SimpleNamespace(track_id=track_id, cls="person",
                            bbox_norm=bbox), {})


def test_counter_visit_open_close_and_merge(db):
    s, store, cam, zone = db
    det = HeatmapDetector()
    inside = [0.4, 0.4, 0.6, 0.6]
    t = 1000.0

    # Person enters the counter zone -> one open visit.
    det._log_counter_presence(_ctx(s, store, cam, zone,
                                   [_person_track(1, inside)]), t)
    visits = s.query(ZoneVisit).all()
    assert len(visits) == 1 and visits[0].exited_at is None

    # Zone empties, but within the 5s grace: visit stays open.
    det._log_counter_presence(_ctx(s, store, cam, zone, []), t + 2)
    det._log_counter_presence(_ctx(s, store, cam, zone,
                                   [_person_track(2, inside)]), t + 4)
    assert s.query(ZoneVisit).count() == 1   # merged, not split

    # Zone empties for longer than the grace: visit closes.
    det._log_counter_presence(_ctx(s, store, cam, zone, []), t + 10)
    det._log_counter_presence(_ctx(s, store, cam, zone, []), t + 16)
    v = s.query(ZoneVisit).one()
    assert v.exited_at is not None
    assert v.duration_seconds is not None and v.duration_seconds >= 0

    # A later customer starts a NEW visit.
    det._log_counter_presence(_ctx(s, store, cam, zone,
                                   [_person_track(3, inside)]), t + 300)
    assert s.query(ZoneVisit).count() == 2


def test_restart_closes_dangling_visit(db):
    """A slice that dies mid-visit leaves exited_at NULL; the next
    slice's first enter closes the dangler instead of stacking opens."""
    s, store, cam, zone = db
    inside = [0.4, 0.4, 0.6, 0.6]
    det1 = HeatmapDetector()
    det1._log_counter_presence(_ctx(s, store, cam, zone,
                                    [_person_track(1, inside)]), 1000.0)
    assert s.query(ZoneVisit).filter(
        ZoneVisit.exited_at.is_(None)).count() == 1

    det2 = HeatmapDetector()                 # restart: empty memory
    det2._log_counter_presence(_ctx(s, store, cam, zone,
                                    [_person_track(9, inside)]), 2000.0)
    open_now = s.query(ZoneVisit).filter(
        ZoneVisit.exited_at.is_(None)).all()
    assert len(open_now) == 1                # exactly one open visit
    assert s.query(ZoneVisit).count() == 2   # dangler was closed


def test_staff_do_not_open_visits(db):
    s, store, cam, zone = db
    det = HeatmapDetector()
    inside = [0.4, 0.4, 0.6, 0.6]
    # Mark the track as staff via the session cache the logger reads.
    det._sessions[(cam.id, 7)] = {"is_staff": True}
    det._log_counter_presence(_ctx(s, store, cam, zone,
                                   [_person_track(7, inside)]), 1000.0)
    assert s.query(ZoneVisit).count() == 0
