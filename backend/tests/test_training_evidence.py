"""Operator verdicts are reviewable evidence, never direct training input."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import Annotation, Dataset, TrainingImage
from app.services import training_evidence as te


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Dataset(name="feedback-person", classes_json=["person"]))
    s.commit()
    yield s
    s.close()


def _img(db, *, kind, target="person", state=te.PENDING, boxed=False, **extra):
    img = TrainingImage(dataset_id=1, file_path="/tmp/x.jpg", labeled=True,
                        example_kind=kind, target_class=target,
                        review_state=state, source_kind="operator_confirmed",
                        source_extra=extra or None)
    db.add(img); db.flush()
    if boxed:
        db.add(Annotation(image_id=img.id, class_label=target or "x",
                          bbox_json=[.5, .5, .2, .4], verified=True))
        db.flush()
    return img


# --- classification of verdicts ----------------------------------------

def test_confirmed_object_detection_is_pending_positive():
    fc = te.classify_feedback("correct", "person")
    assert (fc.kind, fc.target_class, fc.review_state) == ("positive", "person", te.PENDING)


def test_confirmed_behaviour_rule_is_never_a_class():
    fc = te.classify_feedback("correct", "intrusion")
    assert fc.target_class is None and fc.review_state == te.EXCLUDED


def test_not_a_person_on_intrusion_is_person_negative():
    fc = te.classify_feedback("false", "intrusion", "not_a_person")
    assert (fc.kind, fc.target_class) == ("negative", "person")


@pytest.mark.parametrize("reason", ["staff_member", "normal_activity"])
def test_policy_errors_are_never_trained_as_background(reason):
    fc = te.classify_feedback("false", "intrusion", reason)
    assert fc.kind == "policy_error" and fc.review_state == te.EXCLUDED
    assert fc.target_class is None


def test_dismissal_without_reason_is_uncertain():
    fc = te.classify_feedback("false", "loitering", None)
    assert fc.kind == "uncertain" and fc.review_state == te.PENDING


def test_camera_problem_is_excluded():
    assert te.classify_feedback("false", "person", "camera_problem").review_state == te.EXCLUDED


# --- review --------------------------------------------------------------

def test_only_review_makes_evidence_trainable(db):
    img = _img(db, kind="positive", boxed=True)
    assert img.eligible_for_training is False
    te.review(db, img, "approve", reviewer_id=1)
    assert img.review_state == te.APPROVED and img.eligible_for_training is True


def test_uncertain_cannot_be_approved_until_classified(db):
    img = _img(db, kind="uncertain")
    with pytest.raises(te.ReviewError):
        te.review(db, img, "approve", reviewer_id=1)
    te.review(db, img, "approve", reviewer_id=1, kind="negative")
    assert img.eligible_for_training


def test_positive_needs_box_and_negative_must_have_none(db):
    with pytest.raises(te.ReviewError):
        te.review(db, _img(db, kind="positive"), "approve", reviewer_id=1)
    with pytest.raises(te.ReviewError):
        te.review(db, _img(db, kind="negative", boxed=True), "approve", reviewer_id=1)


def test_policy_error_can_never_be_approved(db):
    with pytest.raises(te.ReviewError):
        te.review(db, _img(db, kind="policy_error", target=None), "approve",
                  reviewer_id=1)


def test_optional_second_person_rule(db, monkeypatch):
    monkeypatch.setattr(settings, "training_review_require_different_user", True)
    img = _img(db, kind="positive", boxed=True, verdict_user_id=7)
    with pytest.raises(te.ReviewError):
        te.review(db, img, "approve", reviewer_id=7)
    te.review(db, img, "approve", reviewer_id=8)


def test_reject_and_exclude_are_not_trainable(db):
    a = te.review(db, _img(db, kind="positive", boxed=True), "reject", reviewer_id=1)
    b = te.review(db, _img(db, kind="negative"), "exclude", reviewer_id=1)
    assert not a.eligible_for_training and not b.eligible_for_training


# --- readiness -----------------------------------------------------------

def test_readiness_needs_enough_balanced_approved_samples(db, monkeypatch):
    monkeypatch.setattr(settings, "training_min_approved_positives", 3)
    monkeypatch.setattr(settings, "training_min_approved_negatives", 2)
    monkeypatch.setattr(settings, "training_max_class_imbalance", 2.0)
    for _ in range(3):
        te.review(db, _img(db, kind="positive", boxed=True), "approve", reviewer_id=1)
    _img(db, kind="negative")                      # pending: does not count
    r = te.readiness(db, "person")
    assert not r["ready"] and r["approved_negatives"] == 0 and r["pending_review"] == 1
    for _ in range(2):
        te.review(db, _img(db, kind="negative"), "approve", reviewer_id=1)
    assert te.readiness(db, "person")["ready"]
    for _ in range(4):
        te.review(db, _img(db, kind="positive", boxed=True), "approve", reviewer_id=1)
    r = te.readiness(db, "person")                 # 7:2 > 2:1
    assert not r["ready"] and "exceeds" in r["reasons"][0]


def test_behaviour_rules_are_never_ready():
    r = te.readiness_for_detection_type(None, "intrusion")
    assert r["ready"] is False and r["target_class"] is None


def test_orchestrator_refuses_without_readiness(db):
    from app.training.orchestrator import enqueue_fine_tune_if_due, enqueue_full_retrain
    for fn in (enqueue_fine_tune_if_due, enqueue_full_retrain):
        out = fn(db, "person")
        assert out["status"] == "skipped" and "approved positives" in out["reason"]
    assert fn(db, "intrusion")["status"] == "skipped"


def test_new_rows_default_to_pending_not_approved(db):
    img = TrainingImage(dataset_id=1, file_path="/tmp/y.jpg")
    db.add(img); db.flush()
    assert img.review_state == te.PENDING and img.eligible_for_training is False
