from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Alert, Camera, DeliveryOutbox, DetectionEvent, EvidenceManifest, Incident,
    IncidentMember, IncidentTransition, Store,
)
from app.services.incident_foundations import (
    event_idempotency_key, record_alert_foundations, sync_evidence_manifest,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _alert(db, *, suppressed=False):
    store = Store(name="Test Store", country="Kenya", timezone="Africa/Nairobi")
    db.add(store); db.flush()
    camera = Camera(
        name="Camera 1", brand="generic", connection_type="lan_rtsp",
        host="127.0.0.1", store_id=store.id,
    )
    db.add(camera); db.flush()
    event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=0.9,
        bbox_json=[0.1, 0.1, 0.5, 0.9],
        extra={"priority": "high"}, thumbnail_path="/tmp/frame.jpg",
    )
    db.add(event); db.flush()
    alert = Alert(
        event_id=event.id, status="new",
        notification_suppressed=suppressed,
    )
    db.add(alert); db.flush()
    return store, alert, event


def test_projection_is_idempotent_and_append_only_at_creation():
    db = _session()
    store, alert, event = _alert(db)

    first = record_alert_foundations(
        db, alert, event, store_id=store.id, queue_delivery=True)
    second = record_alert_foundations(
        db, alert, event, store_id=store.id, queue_delivery=True)
    db.commit()

    assert first.id == second.id
    assert db.query(Incident).count() == 1
    assert db.query(IncidentMember).count() == 1
    assert db.query(IncidentTransition).count() == 1
    assert db.query(EvidenceManifest).count() == 1
    assert db.query(DeliveryOutbox).count() == 1
    assert first.current_state == "provisional"
    assert first.severity == "high"
    assert db.query(IncidentMember).one().idempotency_key == event_idempotency_key(event.id)


def test_suppressed_alert_never_enters_delivery_outbox():
    db = _session()
    store, alert, event = _alert(db, suppressed=True)
    record_alert_foundations(
        db, alert, event, store_id=store.id, queue_delivery=True)
    db.commit()

    assert db.query(Incident).count() == 1
    assert db.query(DeliveryOutbox).count() == 0


def test_clip_sync_uses_one_canonical_manifest():
    db = _session()
    store, alert, event = _alert(db)
    record_alert_foundations(db, alert, event, store_id=store.id)
    event.extra = {**(event.extra or {}), "alert_clip_path": "/tmp/alert.mp4"}

    assert sync_evidence_manifest(db, alert, event) is True
    db.commit()
    manifest = db.query(EvidenceManifest).one()
    assert manifest.clip_path == "/tmp/alert.mp4"
    assert manifest.clip_eligible is None
    assert manifest.clip_available is True
    assert manifest.ineligible_reason is None
