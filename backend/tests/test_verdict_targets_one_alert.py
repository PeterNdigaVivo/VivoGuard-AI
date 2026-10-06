"""A True/False verdict changes exactly the alert it was sent for.

Alerts from one camera + type + day are grouped into one card in the UI.
The verdict endpoint takes the alert id from the URL; this pins that the
service never touches the other alerts in the same group.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Alert, AlertReviewDecision, Camera, DetectionEvent, User
from app.services.alert_feedback import record_verdict


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _group(db):
    """Two alerts in one UI group: the newer already True, the older open."""
    cam = Camera(name="Channel 2", site="Test site", brand="dahua",
                 connection_type="nvr_dahua", host="127.0.0.1")
    db.add(cam)
    db.flush()
    user = User(email="op@example.test", password_hash="x", role="operator")
    db.add(user)
    db.flush()
    now = datetime.now(timezone.utc)
    rows = []
    for minutes_ago, status in ((10, "new"), (5, "confirmed")):
        ev = DetectionEvent(camera_id=cam.id, detection_type="person",
                            confidence=.9, bbox_json=[0, 0, 1, 1],
                            timestamp=now - timedelta(minutes=minutes_ago))
        db.add(ev)
        db.flush()
        a = Alert(event_id=ev.id, status=status, created_at=ev.timestamp)
        db.add(a)
        db.flush()
        rows.append(a)
    db.commit()
    older_open, newer_true = rows
    return user, older_open, newer_true


@pytest.mark.parametrize("verdict, expected", [("dismiss", "dismissed"),
                                               ("confirm", "confirmed")])
def test_verdict_changes_only_the_target_alert(db, verdict, expected):
    user, older_open, newer_true = _group(db)
    newer_before = (newer_true.status, newer_true.acknowledged_at,
                    newer_true.assigned_to)

    out = record_verdict(db, older_open.id, verdict, user)

    db.expire_all()
    assert out.id == older_open.id
    assert db.get(Alert, older_open.id).status == expected
    newer = db.get(Alert, newer_true.id)
    assert (newer.status, newer.acknowledged_at, newer.assigned_to) == newer_before
    decisions = db.query(AlertReviewDecision).all()
    assert [(d.alert_id, d.verdict) for d in decisions] == [(older_open.id, expected)]
