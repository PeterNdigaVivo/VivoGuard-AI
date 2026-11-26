"""One canonical fleet: totals, stale/offline handling and health states."""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Camera, Store
from app.services import fleet
from app.services.fleet import CameraInput, classify_camera, summarize

NOW = 1_800_000_000.0
PIPE_OK = {"last_run_ts": NOW - 20, "estimated_full_rotation_seconds": 60}


def _cam(i, **kw):
    base = dict(camera_id=i, name=f"Cam {i}", store_id=1, store_name="Yaya",
                ai_enabled=True, last_frame_at=NOW - 2, fps=2.0,
                inference_last_run=NOW - 30)
    base.update(kw)
    return CameraInput(**base)


def test_classification_boundaries():
    assert classify_camera(_cam(1), now=NOW) == "fresh"
    assert classify_camera(_cam(1, last_frame_at=NOW - 31), now=NOW) == "stale"
    assert classify_camera(_cam(1, last_frame_at=NOW - 599), now=NOW) == "stale"
    assert classify_camera(_cam(1, last_frame_at=NOW - 601), now=NOW) == "offline"
    assert classify_camera(_cam(1, last_frame_at=None), now=NOW) == "offline"
    assert classify_camera(_cam(1, fps=0), now=NOW) == "stale"
    assert classify_camera(_cam(1, ai_enabled=False), now=NOW) == "unconfigured"
    assert classify_camera(_cam(1, store_id=None), now=NOW) == "unconfigured"


def test_totals_always_add_up():
    cams = [_cam(1), _cam(2), _cam(3, last_frame_at=NOW - 120),
            _cam(4, last_frame_at=None), _cam(5, ai_enabled=False)]
    s = summarize(cams, now=NOW, pipeline=PIPE_OK)
    assert s.total == 5
    assert s.fresh + s.stale + s.offline + s.unconfigured == s.total
    assert s.configured == s.fresh + s.stale + s.offline == 4
    assert (s.fresh, s.stale, s.offline, s.unconfigured) == (2, 1, 1, 1)
    assert len(s.coverage_gaps) == 3
    assert [g["state"] for g in s.coverage_gaps] == ["offline", "stale", "unconfigured"]


def test_ai_active_uses_last_run_timestamp():
    cams = [_cam(1), _cam(2, inference_last_run=None),
            _cam(3, inference_last_run=NOW - 3600)]
    assert summarize(cams, now=NOW, pipeline=PIPE_OK).ai_active == 1


def test_health_states():
    assert summarize([_cam(1), _cam(2)], now=NOW, pipeline=PIPE_OK).health == "healthy"
    degraded = summarize([_cam(1), _cam(2, last_frame_at=NOW - 60)], now=NOW,
                         pipeline=PIPE_OK)
    assert degraded.health == "degraded"
    no_pipe = summarize([_cam(1)], now=NOW, pipeline=None)
    assert no_pipe.health == "critical"
    mostly_down = summarize([_cam(1)] + [_cam(i, last_frame_at=None) for i in range(2, 5)],
                            now=NOW, pipeline=PIPE_OK)
    assert mostly_down.health == "critical"
    waiting = summarize([_cam(1)], now=NOW,
                        pipeline={**PIPE_OK, "cameras_waiting_for_worker": 2})
    assert waiting.health == "degraded"


def test_unconfigured_cameras_do_not_degrade_health():
    s = summarize([_cam(1), _cam(2, ai_enabled=False)], now=NOW, pipeline=PIPE_OK)
    assert s.health == "healthy" and s.unconfigured == 1


class _FakeRedis:
    def __init__(self, data):
        self.data = data

    def mget(self, keys):
        return [self.data.get(k) for k in keys]

    def get(self, key):
        return self.data.get(key)


def test_population_excludes_deleted_cameras_and_matches_everywhere():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Store(name="Yaya", country="Kenya")); db.flush()
    for i, deleted in ((1, False), (2, False), (3, True)):
        db.add(Camera(name=f"C{i}", brand="generic", connection_type="lan_rtsp",
                      host="127.0.0.1", store_id=1, ai_enabled=True,
                      is_deleted=deleted))
    db.commit()
    r = _FakeRedis({
        "vg:health:1": json.dumps({"last_frame_at": NOW - 1, "fps": 2}),
        "vg:health:2": json.dumps({"last_frame_at": NOW - 900, "fps": 0}),
        "vg:inference:last-run:1": str(NOW - 10),
        "vg:inference:health": json.dumps(PIPE_OK),
    })
    out = fleet.fleet_status(db, r, now=NOW)
    assert out["totals"]["total"] == 2                 # deleted camera excluded
    assert out["totals"]["fresh"] == 1 and out["totals"]["offline"] == 1
    assert out["totals"]["ai_active"] == 1
    assert out["health"] == "degraded"
    assert out["coverage_gaps"][0]["camera_id"] == 2

    # System Health's cameras section reads the very same numbers.
    from app.utils import system_health
    snap = system_health.cameras_section(db, r, NOW)
    assert snap["total"] == out["totals"]["total"]
    assert snap["streaming"] == out["totals"]["fresh"]
    assert snap["ai_enabled_active"] == out["totals"]["ai_active"]
    assert snap["health"] == out["health"]
    assert system_health.overall_status(
        {"cameras": {"health": "critical"}})[1] == "Critical"
