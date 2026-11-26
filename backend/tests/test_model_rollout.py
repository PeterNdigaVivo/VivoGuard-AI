"""Canary rollout, manual promotion, rollback and the person-model fallback."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models import (
    AIModel, Alert, AlertReviewDecision, Camera, DetectionEvent, Store, User,
)
from app.services import model_rollout as mr

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Store(name="Junction", country="Kenya"))
    s.flush()
    for i in range(6):
        s.add(Camera(name=f"Cam {i}", brand="generic", connection_type="lan_rtsp",
                     host="127.0.0.1", store_id=1))
    s.add(User(email="admin@vivo.test", password_hash="x", role="admin"))
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"pt")
    cls_w = tmp_path / "uniform-cls.pt"
    cls_w.write_bytes(b"pt")
    s.add(AIModel(name="vivo_person", version="v2", base_model="yolov8n.pt",
                  classes_json=["person", "bag"], weights_path=str(weights)))
    s.add(AIModel(name="vivo_intrusion", version="v1", base_model="yolov8n.pt",
                  classes_json=["intrusion"], weights_path=str(weights)))
    s.add(AIModel(name="uniform", version="v1", base_model="yolov8n-cls.pt",
                  classes_json=["person", "no_uniform"], weights_path=str(cls_w)))
    s.commit()
    yield s
    s.close()


def _verdicts(db, cam_id, model_id, at, tp, fp):
    for verdict, n in (("confirmed", tp), ("dismissed", fp)):
        for _ in range(n):
            ev = DetectionEvent(camera_id=cam_id, detection_type="person",
                                confidence=.9, bbox_json=[0, 0, 1, 1],
                                model_id=model_id, timestamp=at)
            db.add(ev); db.flush()
            a = Alert(event_id=ev.id, status=verdict)
            db.add(a); db.flush()
            db.add(AlertReviewDecision(alert_id=a.id, reviewer_id=1, verdict=verdict))
    db.flush()


def test_compatibility_rejects_classifiers_and_models_without_person(db):
    assert mr.person_compatible(db.get(AIModel, 1)) == (True, "ok")
    ok, why = mr.person_compatible(db.get(AIModel, 2))
    assert not ok and "person" in why
    ok, why = mr.person_compatible(db.get(AIModel, 3))
    assert not ok and "classification" in why


def test_canary_is_limited_and_compatible_only(db, monkeypatch):
    monkeypatch.setattr(settings, "model_canary_max_cameras", 2)
    with pytest.raises(mr.RolloutError):
        mr.start_canary(db, 2, [1], user_id=1)          # no person class
    with pytest.raises(mr.RolloutError):
        mr.start_canary(db, 1, [1, 2, 3], user_id=1)    # too many cameras
    r = mr.start_canary(db, 1, [1, 2], user_id=1)
    assert r.stage == "canary"
    assert db.get(Camera, 1).ai_model_id == 1
    assert db.get(Camera, 3).ai_model_id is None        # rest of fleet untouched
    with pytest.raises(mr.RolloutError):
        mr.start_canary(db, 1, [2], user_id=1)          # already in a canary


def test_promotion_needs_a_passed_evaluation_and_is_explicit(db, monkeypatch):
    monkeypatch.setattr(settings, "model_canary_min_reviewed", 10)
    r = mr.start_canary(db, 1, [1], user_id=1)
    r.started_at = NOW - timedelta(hours=24)
    with pytest.raises(mr.RolloutError):
        mr.promote(db, r, [2], user_id=1)               # not evaluated
    # Baseline (base model) 6/10 precise; canary 9/10.
    _verdicts(db, 1, None, NOW - timedelta(hours=30), tp=6, fp=4)
    _verdicts(db, 1, 1, NOW - timedelta(hours=2), tp=9, fp=1)
    ev = mr.evaluate(db, r, now=NOW)
    assert ev.passed and ev.split == "canary"
    assert ev.precision == pytest.approx(0.9)
    assert ev.metrics_json["baseline"]["precision"] == pytest.approx(0.6)
    assert ev.metrics_json["canary"]["per_detector"]["person"]["tp"] == 9
    mr.promote(db, r, [2, 3], user_id=1)
    assert r.stage == "promoted"
    assert [db.get(Camera, i).ai_model_id for i in (1, 2, 3, 4)] == [1, 1, 1, None]


def test_worse_or_thin_canary_cannot_be_promoted(db, monkeypatch):
    monkeypatch.setattr(settings, "model_canary_min_reviewed", 10)
    r = mr.start_canary(db, 1, [1], user_id=1)
    r.started_at = NOW - timedelta(hours=24)
    _verdicts(db, 1, None, NOW - timedelta(hours=30), tp=9, fp=1)
    _verdicts(db, 1, 1, NOW - timedelta(hours=2), tp=5, fp=5)
    ev = mr.evaluate(db, r, now=NOW)
    assert not ev.passed and "below baseline" in ev.reasons_json[0]
    with pytest.raises(mr.RolloutError):
        mr.promote(db, r, [], user_id=1)


def test_rollback_restores_exact_previous_assignment(db):
    db.get(Camera, 2).ai_model_id = 2          # pre-existing (odd) assignment
    db.flush()
    r = mr.start_canary(db, 1, [1, 2], user_id=1)
    mr.rollback(db, r, user_id=1)
    assert db.get(Camera, 1).ai_model_id is None
    assert db.get(Camera, 2).ai_model_id == 2
    assert r.stage == "rolled_back"


def test_fleet_view_shows_base_fallback_for_incompatible_assignment(db):
    db.get(Camera, 4).ai_model_id = 3          # classifier assigned (legacy)
    db.flush()
    view = mr.fleet_model_view(db)
    row = next(c for c in view["cameras"] if c["camera_id"] == 4)
    assert row["effective_model"] == view["base_model"]
    assert "classification" in row["fallback_reason"]


def test_worker_never_loads_incompatible_model_for_person_pipeline(db):
    from app.ai import inference_worker as iw
    cam = db.get(Camera, 5)
    cam.ai_enabled = True
    cam.ai_model_id = 2                        # no 'person' class
    db.commit()
    _cam, _z, _cfg, weights = iw._load_camera_state(db, 5)
    assert weights == settings.default_model
    assert iw.MODEL_USED[5] is None            # attributed to the base model
    cam.ai_model_id = 1
    db.commit()
    _cam, _z, _cfg, weights = iw._load_camera_state(db, 5)
    assert weights.endswith("best.pt") and iw.MODEL_USED[5] == 1


def test_training_records_validation_metrics_without_deploying(db):
    m = db.get(AIModel, 1)
    ev = mr.record_training_evaluation(db, m, {"precision": .8, "recall": .7,
                                               "map50": .75})
    assert ev.split == "validation" and ev.recall == .7
    assert ev.false_positive_rate is None          # not guessed
    assert m.deployed is not True


def test_hourly_auto_promotion_is_disabled():
    from app.tasks.celery_app import celery_app
    from app.tasks.training import evaluate_pending_promotions
    assert evaluate_pending_promotions.run() is None
    beat = celery_app.conf.beat_schedule
    assert all(v["task"] != "training.evaluate_pending_promotions"
               for v in beat.values())
