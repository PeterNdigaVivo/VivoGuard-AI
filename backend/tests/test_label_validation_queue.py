from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.labels import audit_queue, queue, today_counts
from app.config import settings
from app.database import Base
from app.models import (
    Alert, AlertReviewDecision, Camera, DetectionEvent, Store, User,
)


@pytest.fixture(autouse=True)
def _configure_evidence_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "alert_clips").mkdir()


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
        event = DetectionEvent(
            camera_id=camera.id, detection_type="trespass", confidence=.8,
            bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
        )
        db.add(event)
        db.flush()
        alert = Alert(event_id=event.id, status="new")
        db.add(alert)
        db.flush()
        clip_path = tmp_path / "alert_clips" / f"{alert.id}.mp4"
        clip_path.write_bytes(b"playable evidence")
        event.extra = {"alert_clip_path": str(clip_path)}
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


def test_validation_queue_excludes_historical_and_informational_alerts(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(email="reviewer-window@vivo", password_hash="x", role="operator")
    camera = Camera(
        name="Validation Window Camera", brand="dahua",
        connection_type="nvr_dahua", host="127.0.0.1",
    )
    db.add_all([user, camera])
    db.flush()

    for detection_type, created_at in (
        ("intrusion", datetime.now(timezone.utc)),
        ("intrusion", datetime.now(timezone.utc) - timedelta(days=8)),
        ("system_health", datetime.now(timezone.utc)),
    ):
        snapshot = tmp_path / "snapshots" / f"{detection_type}-{created_at.timestamp()}.jpg"
        snapshot.write_bytes(b"evidence")
        event = DetectionEvent(
            camera_id=camera.id, detection_type=detection_type, confidence=.8,
            bbox_json=[0, 0, 1, 1], timestamp=created_at,
            thumbnail_path=str(snapshot),
        )
        db.add(event)
        db.flush()
        db.add(Alert(event_id=event.id, status="new", created_at=created_at))
    db.commit()

    current = queue(
        db=db, _user=user, limit=20, detection_type=None,
        store_id=None, camera_id=None,
    )
    historical = queue(
        db=db, _user=user, limit=20, detection_type=None,
        store_id=None, camera_id=None, include_historical=True,
    )
    counts = today_counts(db=db, _user=user)

    assert len(current) == 1
    assert len(historical) == 2
    assert all(item["detection_type"] == "intrusion" for item in historical)
    assert counts.queue_total == 1
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
    snapshot = tmp_path / "snapshots" / "audit.jpg"
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


@pytest.mark.parametrize("evidence", ["wrong_clip", "empty_thumbnail", "thumbnail_fallback"])
def test_review_queues_follow_playback_evidence(tmp_path, evidence):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        primary = User(email="primary@example.invalid", password_hash="x", role="operator")
        independent = User(email="blind@example.invalid", password_hash="x", role="operator")
        camera = Camera(name="Synthetic camera", brand="dahua",
                        connection_type="nvr_dahua", host="127.0.0.1")
        db.add_all([primary, independent, camera])
        db.flush()
        event = DetectionEvent(
            camera_id=camera.id, detection_type="intrusion", confidence=.8,
            bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
        )
        db.add(event)
        db.flush()
        alert = Alert(event_id=event.id, status="new")
        db.add(alert)
        db.flush()

        if evidence != "empty_thumbnail":
            other = tmp_path / "alert_clips" / f"{alert.id + 1}.mp4"
            other.write_bytes(b"synthetic evidence for a different alert")
            event.extra = {"alert_clip_path": str(other)}
        if evidence != "wrong_clip":
            snapshot = tmp_path / "snapshots" / "evidence.jpg"
            snapshot.write_bytes(b"synthetic thumbnail" if evidence == "thumbnail_fallback" else b"")
            event.thumbnail_path = str(snapshot)
        db.commit()

        primary_cards = queue(
            db=db, _user=primary, limit=20,
            detection_type=None, store_id=None, camera_id=None,
        )
        alert.status = "confirmed"
        db.add(AlertReviewDecision(alert_id=alert.id, reviewer_id=primary.id, verdict="confirmed"))
        db.commit()
        blind_cards = audit_queue(db=db, user=independent, limit=20)

        for cards in (primary_cards, blind_cards):
            if evidence == "thumbnail_fallback":
                assert len(cards) == 1
                assert cards[0]["snapshot_url"] == f"/api/alerts/{alert.id}/snapshot"
                assert cards[0]["clip_url"] is None
                assert cards[0]["clip_available"] is False
            else:
                assert cards == []
