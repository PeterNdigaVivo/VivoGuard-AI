"""Direct label promotion: one operator mark is the training label, with
the trust/agreement gate, the burst guard, the audit sample and the
Unclear / Bad image buttons."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.alert_feedback as fb
import app.tasks.feedback_absorb as fa
import app.training.feedback_loop as fl
from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user
from app.models import (
    Alert, AlertReviewDecision, Annotation, Camera, DetectionEvent,
    GovernanceAuditLog, Store, TrainingImage, User,
)
from app.services import training_evidence as te


@pytest.fixture()
def Session(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path / "datasets"))
    monkeypatch.setattr(settings, "training_direct_promotion_enabled", True)
    # The alert-quality breaker is tested elsewhere; on SQLite its keyset
    # pager re-reads the same row (text dates), so keep it out of the way.
    monkeypatch.setattr(settings, "alert_quality_control_enabled", False)
    monkeypatch.setattr(settings, "label_audit_sample_rate", 0.0)
    monkeypatch.setattr(settings, "label_burst_count", 15)
    monkeypatch.setattr(settings, "label_burst_window_minutes", 10)
    monkeypatch.setattr(fl, "_enqueue_preview", lambda *a, **k: None)
    monkeypatch.setattr(fl, "_enqueue_temporal_harvest", lambda *a, **k: None)
    # The verdict's training step runs inline instead of on Celery.
    monkeypatch.setattr(fb, "_enqueue_absorb",
                        lambda aid, verdict: fa.absorb_verdict_now(aid, verdict, S))
    with S() as db:
        db.add(Store(name="Yaya", country="Kenya")); db.flush()
        db.add(Camera(name="Door", brand="generic", connection_type="lan_rtsp",
                      host="127.0.0.1", store_id=1))
        db.add(User(email="op@vivo.test", password_hash="x", role="operator"))
        db.add(User(email="lead@vivo.test", password_hash="x", role="operator",
                    trusted_labeller=True))
        db.commit()
    S.tmp = tmp_path
    return S


def _alert(S, *, dtype="intrusion", conf=0.9, ai=None, ai_conf=None) -> int:
    snap = S.tmp / f"snap_{datetime.now().timestamp()}.jpg"
    snap.write_bytes(b"\xff\xd8\xff jpeg")
    with S() as db:
        ev = DetectionEvent(camera_id=1, detection_type=dtype, confidence=conf,
                            bbox_json=[.1, .2, .3, .8], thumbnail_path=str(snap),
                            timestamp=datetime.now(timezone.utc))
        db.add(ev); db.flush()
        a = Alert(event_id=ev.id, status="new", ai_verdict=ai, ai_confidence=ai_conf)
        db.add(a); db.commit()
        return a.id


def _mark(S, alert_id, verdict, *, user_email="op@vivo.test", reason=None):
    with S() as db:
        user = db.query(User).filter_by(email=user_email).one()
        if reason:
            db.get(Alert, alert_id).dismiss_reason = reason
        fb.record_verdict(db, alert_id, verdict, user)


def _row(S, alert_id) -> TrainingImage:
    with S() as db:
        img = db.query(TrainingImage).filter_by(label_alert_id=alert_id).one()
        db.expunge(img)
        return img


# --- direct promotion --------------------------------------------------

def test_agreeing_true_mark_becomes_a_trained_positive(Session):
    aid = _alert(Session, conf=0.9)
    _mark(Session, aid, "confirm")
    img = _row(Session, aid)
    assert (img.example_kind, img.target_class) == ("positive", "person")
    assert img.promotion_path == "auto_agreement"
    assert img.review_state == te.APPROVED and img.eligible_for_training
    with Session() as db:
        assert db.query(Annotation).filter_by(image_id=img.id).one().class_label == "person"
        assert te.readiness(db, "person")["approved_positives"] == 1   # panel moves
        audit = db.query(GovernanceAuditLog).filter_by(entity_id=str(img.id)).one()
        assert audit.action == "training_label.auto_agreement"
        assert audit.details["mark"] == "confirm"
    assert img.labelled_by_user_id is not None and img.labelled_at is not None


def test_switch_off_keeps_every_label_waiting_for_review(Session, monkeypatch):
    monkeypatch.setattr(settings, "training_direct_promotion_enabled", False)
    aid = _alert(Session, conf=0.95)
    _mark(Session, aid, "confirm")
    img = _row(Session, aid)
    assert img.promotion_path == "pending_manual" and not img.eligible_for_training


# --- agreement gate ----------------------------------------------------

def test_disagreement_with_the_detector_is_held_for_review(Session):
    aid = _alert(Session, conf=0.9)                      # model is sure: real
    _mark(Session, aid, "dismiss", reason="not_a_person")
    img = _row(Session, aid)
    assert img.promotion_path == "held_disagreement"
    assert img.review_state == te.PENDING and not img.eligible_for_training
    assert img.gate_json["model"]["source"] == "detector_confidence"


def test_uncertain_confidence_band_is_a_disagreement(Session):
    aid = _alert(Session, conf=0.5)                      # between 0.45 and 0.60
    _mark(Session, aid, "confirm")
    assert _row(Session, aid).promotion_path == "held_disagreement"


def test_low_confidence_false_agrees(Session):
    aid = _alert(Session, conf=0.30)
    _mark(Session, aid, "dismiss", reason="not_a_person")
    img = _row(Session, aid)
    assert (img.promotion_path, img.example_kind, img.target_class) == (
        "auto_agreement", "negative", "person")


def test_ai_second_opinion_is_used_whenever_present(Session):
    aid = _alert(Session, conf=0.95, ai="false_alert", ai_conf=0.9)
    _mark(Session, aid, "dismiss", reason="not_a_person")
    img = _row(Session, aid)
    assert img.promotion_path == "auto_agreement"
    assert img.gate_json["model"]["source"] == "ai_second_opinion"


def test_trusted_labeller_skips_the_agreement_gate(Session):
    aid = _alert(Session, conf=0.9)
    _mark(Session, aid, "dismiss", reason="not_a_person", user_email="lead@vivo.test")
    assert _row(Session, aid).promotion_path == "auto_trusted"


# --- the person-based two-choice False ---------------------------------

def test_real_person_rule_wrong_is_a_policy_error_never_trained(Session):
    aid = _alert(Session, conf=0.3)
    _mark(Session, aid, "dismiss", reason="real_person_rule_wrong",
          user_email="lead@vivo.test")
    img = _row(Session, aid)
    assert (img.example_kind, img.promotion_path) == ("policy_error", "policy_error")
    assert not img.eligible_for_training


def test_plain_false_on_a_person_alert_is_held_as_ambiguous(Session):
    aid = _alert(Session, conf=0.3)
    _mark(Session, aid, "dismiss", user_email="lead@vivo.test")
    img = _row(Session, aid)
    assert img.promotion_path == "held_ambiguous" and not img.eligible_for_training


# --- burst guard -------------------------------------------------------

def test_mass_marking_is_held_including_the_earlier_marks(Session, monkeypatch):
    monkeypatch.setattr(settings, "label_burst_count", 4)
    ids = [_alert(Session, conf=0.2) for _ in range(5)]
    for aid in ids:
        _mark(Session, aid, "dismiss", reason="not_a_person")
    paths = [_row(Session, aid).promotion_path for aid in ids]
    assert paths == ["held_burst"] * 5               # earlier 3 held retroactively
    with Session() as db:
        assert te.readiness(db, "person")["approved_negatives"] == 0


def test_marks_spread_over_time_are_not_a_burst(Session, monkeypatch):
    monkeypatch.setattr(settings, "label_burst_count", 3)
    ids = [_alert(Session, conf=0.2) for _ in range(3)]
    for n, aid in enumerate(ids):
        _mark(Session, aid, "dismiss", reason="not_a_person")
        with Session() as db:   # push each earlier decision out of the window
            for d in db.query(AlertReviewDecision).all():
                d.created_at = datetime.now(timezone.utc) - timedelta(minutes=30 * (len(ids) - n))
            db.commit()
    assert {_row(Session, aid).promotion_path for aid in ids} == {"auto_agreement"}


def test_burst_counting_is_per_person_and_per_direction(Session, monkeypatch):
    monkeypatch.setattr(settings, "label_burst_count", 3)
    with Session() as db:
        db.add(User(email="op2@vivo.test", password_hash="x", role="operator"))
        db.commit()
    a = [_alert(Session, conf=0.2) for _ in range(2)]
    b = [_alert(Session, conf=0.2) for _ in range(2)]
    t = _alert(Session, conf=0.9)
    for aid in a:
        _mark(Session, aid, "dismiss", reason="not_a_person")
    for aid in b:
        _mark(Session, aid, "dismiss", reason="not_a_person", user_email="op2@vivo.test")
    _mark(Session, t, "confirm")                 # same person, other direction
    assert {_row(Session, x).promotion_path for x in a + b + [t]} == {"auto_agreement"}


# --- audit sample ------------------------------------------------------

def test_audit_sample_still_trains_and_shows_in_review(Session, monkeypatch):
    monkeypatch.setattr(settings, "label_audit_sample_rate", 1.0)
    aid = _alert(Session, conf=0.9)
    _mark(Session, aid, "confirm")
    img = _row(Session, aid)
    assert img.audit_sample and img.eligible_for_training


# --- re-marking is idempotent -------------------------------------------

def test_re_mark_updates_the_one_training_row(Session):
    aid = _alert(Session, conf=0.2)
    _mark(Session, aid, "confirm")                       # disagrees -> held
    first = _row(Session, aid)
    _mark(Session, aid, "dismiss", reason="not_a_person")    # agrees -> trained
    second = _row(Session, aid)
    assert first.id == second.id
    with Session() as db:
        assert db.query(TrainingImage).filter_by(source_alert_id=aid).count() == 1
        assert db.query(Annotation).filter_by(image_id=second.id).count() == 0
    assert (second.example_kind, second.promotion_path) == ("negative", "auto_agreement")


def test_undo_keeps_the_row_but_stops_it_training(Session):
    aid = _alert(Session, conf=0.9)
    _mark(Session, aid, "confirm")
    with Session() as db:
        fl.revert_verdict(db, aid)
    img = _row(Session, aid)
    assert img.promotion_path == "reverted" and not img.eligible_for_training


# --- Unclear / Bad image buttons and the review queue -------------------

@pytest.fixture()
def client(Session):
    from app.api.alerts import router as alerts_router
    from app.api.training_evidence import router as ev_router
    app = FastAPI()
    app.include_router(alerts_router)
    app.include_router(ev_router)

    def _db():
        with Session() as s:
            yield s
    app.dependency_overrides[get_db] = _db
    with Session() as s:
        u = s.query(User).filter_by(email="op@vivo.test").one()
        s.expunge(u)
    app.dependency_overrides[get_current_user] = lambda: u
    return TestClient(app)


@pytest.mark.parametrize("reason, path", [("unclear", "excluded_unclear"),
                                          ("bad_image", "excluded_bad_image")])
def test_unclear_and_bad_image_close_and_exclude(Session, client, reason, path):
    aid = _alert(Session, conf=0.9)
    r = client.post(f"/alerts/{aid}/exclude-from-training", json={"reason": reason})
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    img = _row(Session, aid)
    assert img.promotion_path == path and img.review_state == te.EXCLUDED
    assert not img.eligible_for_training
    with Session() as db:
        # Not a True/False verdict: no effect on detector accuracy.
        assert db.query(AlertReviewDecision).count() == 0
        assert db.get(Alert, aid).dismiss_reason == reason


def test_exceptions_reach_training_review_and_can_be_decided(Session, client, monkeypatch):
    monkeypatch.setattr(settings, "label_audit_sample_rate", 1.0)
    held = _alert(Session, conf=0.9)
    _mark(Session, held, "dismiss", reason="not_a_person")          # disagreement
    audited = _alert(Session, conf=0.9)
    _mark(Session, audited, "confirm")                              # audit sample
    page = client.get("/training-evidence", params={"bucket": "disagreement"}).json()
    assert [i["source_alert_id"] for i in page["items"]] == [held]
    assert page["counts"]["disagreement"] == 1 and page["counts"]["audit"] == 1
    audit_item = client.get("/training-evidence", params={"bucket": "audit"}).json()["items"][0]
    assert audit_item["source_alert_id"] == audited
    # Reviewer confirms the held negative; it trains and leaves the queue.
    r = client.post(f"/training-evidence/{page['items'][0]['id']}/review",
                    json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["promotion_path"] == "reviewed"
    r = client.post(f"/training-evidence/{audit_item['id']}/review",
                    json={"decision": "approve"})
    assert r.json()["promotion_path"] == "audited_ok"
    counts = client.get("/training-evidence").json()["counts"]
    assert counts["disagreement"] == 0 and counts["audit"] == 0
