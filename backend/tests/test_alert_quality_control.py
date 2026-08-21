from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Alert, AlertQualityControl, Camera, DetectionEvent, User
from app.services.alert_feedback import record_verdict
from app.services.alert_quality import (
    apply_quality_control, quality_scorecards, refresh_pair_control,
    set_manual_mode,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _camera(db):
    cam = Camera(name="Junction Channel 1", site="Vivo Junction",
                 brand="dahua", connection_type="nvr_dahua",
                 host="127.0.0.1")
    db.add(cam)
    db.flush()
    return cam


def _alert(db, cam, status="new", *, clip=False, age_minutes=0):
    ev = DetectionEvent(camera_id=cam.id, detection_type="intrusion",
                        confidence=.9, bbox_json=[0, 0, 1, 1],
                        clip_path="/clip.mp4" if clip else None,
                        timestamp=datetime.now(timezone.utc)
                                  - timedelta(minutes=age_minutes))
    db.add(ev)
    db.flush()
    alert = Alert(event_id=ev.id, status=status,
                  created_at=ev.timestamp)
    db.add(alert)
    db.flush()
    return alert, ev


def test_false_rate_opens_quarantine_and_does_not_auto_recover(db):
    cam = _camera(db)
    for index in range(20):
        _alert(db, cam, "dismissed" if index < 10 else "confirmed",
               age_minutes=index)
    state = refresh_pair_control(db, cam.id, "intrusion")
    assert state.mode == "quarantined"
    assert state.last_sample_size == 20
    assert state.last_false_rate == pytest.approx(.5)

    # A later good sample does not silently re-enable notifications.
    _alert(db, cam, "confirmed")
    state = refresh_pair_control(db, cam.id, "intrusion")
    assert state.mode == "quarantined"


def test_quarantined_alert_retains_evidence_but_suppresses_escalation(db):
    cam = _camera(db)
    state = AlertQualityControl(camera_id=cam.id,
                                detection_type="intrusion",
                                mode="quarantined", reason="poor precision")
    db.add(state)
    alert, event = _alert(db, cam, clip=True)
    apply_quality_control(db, alert, event)

    assert alert.id is not None
    assert event.clip_path == "/clip.mp4"
    assert alert.review_only is True
    assert alert.notification_suppressed is True
    assert alert.training_eligible is False
    assert event.extra["quality_control"]["training_eligible"] is False


def test_release_is_evidence_gated_but_force_is_attributed(db):
    cam = _camera(db)
    state = AlertQualityControl(
        camera_id=cam.id, detection_type="intrusion", mode="quarantined",
        reviewed_count_at_quarantine=0, reason="poor precision")
    db.add(state)
    for _ in range(5):
        _alert(db, cam, "confirmed")
    with pytest.raises(ValueError, match="20 post-quarantine"):
        set_manual_mode(db, cam.id, "intrusion", "active",
                        reason="walkthrough looked good", actor="admin@vivo")
    released = set_manual_mode(
        db, cam.id, "intrusion", "active",
        reason="Emergency admin release after physical acceptance test",
        actor="admin@vivo", force=True)
    assert released.mode == "active"
    assert released.source == "manual"
    assert released.changed_by == "admin@vivo"


def test_scorecard_is_honest_about_precision_recall_and_agreement(db):
    cam = _camera(db)
    _alert(db, cam, "confirmed", clip=True)
    _alert(db, cam, "dismissed")
    _alert(db, cam, "new")
    cards = quality_scorecards(db)
    card = cards[0]
    assert card["true_alerts"] == 1
    assert card["false_alerts"] == 1
    assert card["unreviewed_alerts"] == 1
    assert card["reviewed_sample_size"] == 2
    assert card["precision"] == pytest.approx(.5)
    assert card["recall"] is None
    assert "missed events" in card["recall_limitation"]
    assert card["incident_clips_available"] == 1
    assert card["clip_availability_rate"] == pytest.approx(1 / 3)
    assert card["reviewer_agreement"] is None


def test_threshold_crossing_verdict_is_not_sent_to_training(db, monkeypatch):
    cam = _camera(db)
    for index in range(19):
        _alert(db, cam, "dismissed" if index < 9 else "confirmed",
               age_minutes=index + 1)
    current, _ = _alert(db, cam)
    user = User(email="reviewer@vivo", password_hash="x", role="operator")
    db.add(user)
    db.commit()
    called = []
    monkeypatch.setattr("app.training.feedback_loop.mark_dismissed",
                        lambda *_args: called.append(True))

    record_verdict(db, current.id, "dismiss", user)
    db.refresh(current)
    assert current.review_only is True
    assert current.notification_suppressed is True
    assert current.training_eligible is False
    assert called == []
