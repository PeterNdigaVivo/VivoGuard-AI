"""The live inference path: policy decision + one incident per event."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai import inference_worker as iw
from app.config import settings
from app.database import Base
from app.models import Alert, Camera, DetectionEvent, Store, Zone
from app.models.incident import Incident
from app.services import incidents as svc


@pytest.fixture()
def Session(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "incident_lifecycle_enabled", True)
    monkeypatch.setattr(settings, "verifier_enabled", False)
    monkeypatch.setattr(settings, "vlm_enabled", False)
    import app.tasks.alert_snapshots as snaps
    monkeypatch.setattr(snaps.schedule_alert_filmstrip, "delay",
                        lambda *a, **k: None)
    svc._reset_schema_cache()
    yield sessionmaker(bind=engine)
    svc._reset_schema_cache()


@pytest.fixture()
def world(Session):
    with Session() as db:
        store = Store(name="Yaya", country="Kenya", timezone="Africa/Nairobi",
                      business_hours_json={"mon": [["09:30", "20:00"]]})
        db.add(store); db.flush()
        cam = Camera(name="Stockroom", brand="generic", connection_type="lan_rtsp",
                     host="127.0.0.1", store_id=store.id)
        db.add(cam); db.flush()
        zone = Zone(camera_id=cam.id, name="Stock", polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
                    detection_types_json=["restricted"])
        db.add(zone); db.commit()
        return {"store": store.id, "cam": cam.id, "zone": zone.id}


def _ev(world, **extra):
    return SimpleNamespace(
        detection_type="intrusion", zone_id=world["zone"], confidence=0.9,
        bbox_norm=[.1, .1, .5, .9], cls="person", track_id=4,
        extra={"time_context": "after_hours", "after_hours": True,
               "rule": "after_hours", "priority": "high", **extra})


def test_continuing_intrusion_is_one_incident_and_notifies_once(Session, world):
    emitted = []
    with Session() as db:
        for _ in range(4):
            _eid, alert_id = iw._persist_event(
                db, world["cam"], _ev(world), None, store_id=world["store"],
                frames_seen=8)
            emitted.append(alert_id)
        db.commit()
        assert db.query(Alert).count() == 4          # audit trail kept
        inc = db.query(Incident).one()
        assert inc.event_count == 4 and inc.priority == "urgent"
        assert inc.policy_reason == "confirmed_after_hours_intrusion"
        ev = db.query(DetectionEvent).first()
        assert ev.extra["policy"]["decision"] == "incident"
    assert emitted[0] is not None
    assert emitted[1:] == [None, None, None]         # only the first notifies


def test_unconfirmed_track_is_discarded_without_alert_row(Session, world):
    with Session() as db:
        _eid, alert_id = iw._persist_event(
            db, world["cam"], _ev(world), None, store_id=world["store"],
            suppress_alert=True, suppress_reason="not_confirmed_across_frames",
            frames_seen=1)
        db.commit()
        assert alert_id is None
        assert db.query(Alert).count() == 0
        assert db.query(Incident).count() == 0
        ev = db.query(DetectionEvent).one()
        assert ev.extra["policy"]["decision"] == "discarded"


def test_early_staff_goes_to_review_without_notification(Session, world):
    with Session() as db:
        _eid, alert_id = iw._persist_event(
            db, world["cam"], _ev(world, time_context="before_hours",
                                  staff_level="high", after_hours=False),
            None, store_id=world["store"], frames_seen=8)
        db.commit()
        assert alert_id is None
        inc = db.query(Incident).one()
        assert inc.queue == "review" and inc.priority == "low"
        assert db.query(Alert).one().review_only


def test_lifecycle_off_keeps_legacy_behaviour(Session, world, monkeypatch):
    monkeypatch.setattr(settings, "incident_lifecycle_enabled", False)
    with Session() as db:
        ids = [iw._persist_event(db, world["cam"], _ev(world), None,
                                 store_id=world["store"], frames_seen=8)[1]
               for _ in range(2)]
        db.commit()
        assert all(ids)
        assert db.query(Incident).count() == 0
        assert db.query(Alert).filter(Alert.incident_id.isnot(None)).count() == 0
