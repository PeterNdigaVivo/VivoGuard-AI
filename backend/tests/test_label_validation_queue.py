from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.labels import audit_queue, queue
from app.database import Base
from app.models import (
    Alert, AlertReviewDecision, Camera, DetectionEvent, Store, User,
)


def test_validation_queue_is_pinned_to_exact_camera_and_requires_retained_clip(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    store = Store(name="Vivo Junction", country="Kenya")
    user = User(email="reviewer@vivo", password_hash="x", role="operator")
    db.add_all([store, user])
    db.flush()
    cameras = [
        Camera(name=f"Junction Ch{channel}", brand="dahua",
               connection_type="nvr_dahua", host="127.0.0.1",
               store_id=store.id)
        for channel in (1, 5)
    ]
    db.add_all(cameras)
    db.flush()
    for camera in cameras:
        clip_path = tmp_path / f"{camera.id}.mp4"
        clip_path.write_bytes(b"playable evidence")
        event = DetectionEvent(
            camera_id=camera.id, detection_type="trespass", confidence=.8,
            bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
            extra={"alert_clip_path": str(clip_path)},
        )
        db.add(event)
        db.flush()
        db.add(Alert(event_id=event.id, status="new"))
    db.commit()

    rows = queue(
        db=db, _user=user, limit=20, detection_type="trespass",
        store_id=store.id, camera_id=cameras[1].id,
    )

    assert len(rows) == 1
    assert rows[0]["camera_id"] == cameras[1].id
    assert rows[0]["clip_available"] is True
    db.close()


def test_validation_queue_excludes_alerts_without_viewable_evidence(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(email="reviewer@vivo", password_hash="x", role="operator")
    camera = Camera(
        name="Missing Evidence Camera", brand="dahua",
        connection_type="nvr_dahua", host="127.0.0.1",
    )
    db.add_all([user, camera])
    db.flush()
    event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=.8,
        bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
        thumbnail_path=str(tmp_path / "pruned.jpg"),
        extra={"alert_clip_path": str(tmp_path / "pruned.mp4")},
    )
    db.add(event)
    db.flush()
    db.add(Alert(event_id=event.id, status="new"))
    db.commit()

    assert queue(
        db=db, _user=user, limit=20, detection_type=None,
        store_id=None, camera_id=None,
    ) == []
    db.close()


def test_audit_queue_is_blind_and_excludes_the_same_reviewer(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    primary = User(email="primary@vivo", password_hash="x", role="operator")
    independent = User(
        email="independent@vivo", password_hash="x", role="operator",
    )
    camera = Camera(
        name="Audit Camera", brand="dahua", connection_type="nvr_dahua",
        host="127.0.0.1",
    )
    db.add_all([primary, independent, camera])
    db.flush()
    snapshot = tmp_path / "audit.jpg"
    snapshot.write_bytes(b"jpeg evidence")
    event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=.8,
        bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
        thumbnail_path=str(snapshot),
    )
    db.add(event)
    db.flush()
    alert = Alert(event_id=event.id, status="confirmed")
    db.add(alert)
    db.flush()
    db.add(AlertReviewDecision(
        alert_id=alert.id, reviewer_id=primary.id, verdict="confirmed",
    ))
    db.commit()

    rows = audit_queue(db=db, user=independent, limit=20)
    assert len(rows) == 1
    assert rows[0]["status"] == "independent_review_pending"
    assert rows[0]["review_reason"] == "blind independent review"
    assert audit_queue(db=db, user=primary, limit=20) == []
    db.close()
