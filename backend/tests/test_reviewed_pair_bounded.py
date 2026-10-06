"""The bounded _reviewed_pair query must give exactly the old answer.

The old implementation loaded every reviewed alert of a camera/type pair
and trimmed to the newest 50 in Python, so a True/False click got slower
as history grew. `_old_reviewed_pair` below is that implementation, kept
here as the reference the new query is checked against.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Alert, AlertReviewDecision, Camera, DetectionEvent, User
from app.services.alert_quality import _reviewed_pair, pair_metrics


def _old_reviewed_pair(db, camera_id, detection_type, *, limit=50):
    rows = (db.query(Alert, AlertReviewDecision)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .outerjoin(AlertReviewDecision, AlertReviewDecision.alert_id == Alert.id)
              .filter(DetectionEvent.camera_id == camera_id,
                      DetectionEvent.detection_type == detection_type,
                      or_(Alert.status.in_(("confirmed", "dismissed")),
                          AlertReviewDecision.id.is_not(None)))
              .order_by(Alert.created_at.desc(), Alert.id.desc(),
                        AlertReviewDecision.created_at, AlertReviewDecision.id).all())
    alerts, verdicts, order = {}, {}, []
    for alert, decision in rows:
        if alert.id not in alerts:
            alerts[alert.id] = alert
            order.append(alert.id)
        if decision is not None:
            verdicts[alert.id] = decision.verdict
    reviewed = []
    for alert_id in order:
        verdict = verdicts.get(alert_id, alerts[alert_id].status)
        if verdict in {"confirmed", "dismissed"}:
            reviewed.append((alerts[alert_id], verdict))
        if len(reviewed) >= limit:
            break
    return reviewed


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed(db, rng, n):
    cams = []
    for i in range(2):
        cam = Camera(name=f"c{i}", site="s", brand="dahua",
                     connection_type="nvr_dahua", host="127.0.0.1")
        db.add(cam)
        db.flush()
        cams.append(cam)
    users = []
    for i in range(2):
        u = User(email=f"r{i}@example.test", password_hash="x", role="operator")
        db.add(u)
        db.flush()
        users.append(u)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(n):
        cam = rng.choice(cams)
        dtype = rng.choice(["person", "staff_present"])
        ts = base + timedelta(minutes=rng.randint(0, 5000))   # ties on purpose
        ev = DetectionEvent(camera_id=cam.id, detection_type=dtype, confidence=.9,
                            bbox_json=[0, 0, 1, 1], timestamp=ts)
        db.add(ev)
        db.flush()
        status = rng.choice(["new", "confirmed", "dismissed", "resolved"])
        a = Alert(event_id=ev.id, status=status, created_at=ts)
        db.add(a)
        db.flush()
        # Some alerts carry one or more decisions (latest wins), including
        # "new"/"resolved" ones whose lifecycle status no longer says it.
        for k in range(rng.choice([0, 0, 1, 2])):
            db.add(AlertReviewDecision(
                alert_id=a.id, reviewer_id=users[k % 2].id,
                verdict=rng.choice(["confirmed", "dismissed", "pending"]),
                created_at=ts + timedelta(seconds=k + 1)))
    db.commit()
    return cams


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("limit", [3, 50])
def test_matches_the_old_unbounded_implementation(db, seed, limit):
    rng = random.Random(seed)
    cams = _seed(db, rng, 400)
    for cam in cams:
        for dtype in ("person", "staff_present"):
            old = [(a.id, v) for a, v in _old_reviewed_pair(db, cam.id, dtype, limit=limit)]
            new = [(a.id, v) for a, v in _reviewed_pair(db, cam.id, dtype, limit=limit)]
            assert new == old


def test_pair_metrics_unchanged_on_large_history(db):
    rng = random.Random(7)
    cams = _seed(db, rng, 600)
    for cam in cams:
        old = _old_reviewed_pair(db, cam.id, "person")
        metrics = pair_metrics(db, cam.id, "person")
        assert metrics["sample_size"] == len(old)
        assert metrics["false_alerts"] == sum(v == "dismissed" for _a, v in old)
