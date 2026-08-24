from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Alert, Camera, DeliveryOutbox, DetectionEvent, EvidenceManifest, Incident,
    IncidentMember, IncidentTransition, RecordingClip, Store,
)
from app.services.incident_foundations import (
    acknowledge_incident, event_idempotency_key, record_alert_foundations,
    resolve_incident, sync_evidence_manifest, transition_incident,
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
    assert first.evaluation_state == "provisional"
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
    assert manifest.clip_eligible is True
    assert manifest.clip_available is True
    assert manifest.ineligible_reason is None


def test_recording_window_sets_honest_clip_eligibility_before_extraction():
    db = _session()
    store, alert, event = _alert(db)
    db.add(RecordingClip(
        camera_id=event.camera_id, store_id=store.id, window_id="test-window",
        file_path="/tmp/window.mp4", started_at=event.timestamp,
        status="recording",
    ))
    db.flush()

    record_alert_foundations(db, alert, event, store_id=store.id)
    manifest = db.query(EvidenceManifest).one()

    assert manifest.clip_eligible is True
    assert manifest.clip_available is False
    assert manifest.ineligible_reason is None


def test_alert_without_recording_is_excluded_from_clip_sla_denominator():
    db = _session()
    store, alert, event = _alert(db)
    record_alert_foundations(db, alert, event, store_id=store.id)
    manifest = db.query(EvidenceManifest).one()

    assert manifest.clip_eligible is False
    assert manifest.clip_available is False
    assert manifest.ineligible_reason == "no_recording_coverage"


def test_evaluation_and_operational_states_are_independent():
    db = _session()
    store, alert, event = _alert(db)
    incident = record_alert_foundations(db, alert, event, store_id=store.id)

    transition_incident(
        db, incident, to_state="verified", actor_type="reviewer",
        actor_id="reviewer-1", reason_code="evidence_confirmed",
    )
    acknowledge_incident(
        db, incident, actor_id="operator-1", reason_code="operator_reviewing",
    )
    resolve_incident(
        db, incident, actor_id="operator-1", reason_code="response_complete",
    )
    db.commit()

    assert incident.evaluation_state == "verified"
    assert incident.acknowledged_at is not None
    assert incident.resolved_at is not None
    assert incident.version == 4
    assert [r.to_state for r in db.query(IncidentTransition).order_by(IncidentTransition.id)] == [
        "provisional", "verified", "acknowledged", "resolved",
    ]


def test_invalid_transition_and_resolve_before_ack_are_rejected():
    db = _session()
    store, alert, event = _alert(db)
    incident = record_alert_foundations(db, alert, event, store_id=store.id)

    try:
        transition_incident(
            db, incident, to_state="provisional", actor_type="system",
            reason_code="invalid",
        )
        raise AssertionError("invalid transition was accepted")
    except ValueError:
        pass
    try:
        resolve_incident(
            db, incident, actor_id="operator-1", reason_code="too_early",
        )
        raise AssertionError("resolution before acknowledgement was accepted")
    except ValueError:
        pass
