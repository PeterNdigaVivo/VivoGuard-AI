from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.operations import (
    DisagreementAdjudicationIn,
    adjudicate_reviewer_disagreement,
    verify_missed_event_training,
)
from app.database import Base
from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, Annotation, AssuranceCase,
    Camera, Dataset, DetectionEvent, GovernanceAuditLog, TrainingImage, User,
)


def _review_disagreement_fixture(db: Session, *, quality_mode: str = "active"):
    primary = User(email="primary@example.test", password_hash="x", role="operator")
    independent = User(email="independent@example.test", password_hash="x", role="operator")
    adjudicator = User(email="adjudicator@example.test", password_hash="x", role="operator")
    camera = Camera(name="Camera", brand="dahua", connection_type="nvr_dahua",
                    host="127.0.0.1")
    dataset = Dataset(name="feedback-intrusion", classes_json=["intrusion"])
    db.add_all([primary, independent, adjudicator, camera, dataset])
    db.flush()
    event = DetectionEvent(camera_id=camera.id, detection_type="intrusion",
                           confidence=.9, bbox_json=[0, 0, 1, 1],
                           timestamp=datetime.now(timezone.utc))
    db.add(event); db.flush()
    alert = Alert(event_id=event.id, status="confirmed", review_only=True,
                  training_eligible=False)
    db.add(alert); db.flush()
    db.add_all([
        AlertReviewDecision(alert_id=alert.id, reviewer_id=primary.id,
                            verdict="confirmed"),
        AlertReviewDecision(alert_id=alert.id, reviewer_id=independent.id,
                            verdict="dismissed", classification="independent_disagreement"),
        TrainingImage(dataset_id=dataset.id, camera_id=camera.id,
                      file_path="/evidence/frame.jpg", labeled=True,
                      source_alert_id=alert.id, source_kind="operator_confirmed",
                      eligible_for_training=False, review_state="quarantined"),
    ])
    if quality_mode != "active":
        db.add(AlertQualityControl(camera_id=camera.id,
                                   detection_type="intrusion", mode=quality_mode))
    case = AssuranceCase(
        dedup_key=f"review-disagreement:{alert.id}",
        case_type="reviewer_disagreement", severity="high", status="open",
        title="Reviewer disagreement", alert_id=alert.id, event_id=event.id,
        evidence={"primary_verdict": "confirmed",
                  "independent_verdict": "dismissed"},
        training_status="blocked_pending_adjudication",
    )
    db.add(case); db.commit()
    return primary, adjudicator, alert, case


def test_third_reviewer_adjudication_promotes_evidence_and_preserves_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _primary, adjudicator, alert, case = _review_disagreement_fixture(db)
        result = adjudicate_reviewer_disagreement(
            case.id,
            DisagreementAdjudicationIn(
                verdict="dismiss", rationale="The clip shows a static mannequin."),
            db=db, user=adjudicator,
        )

        db.refresh(alert); db.refresh(case)
        image = db.query(TrainingImage).filter_by(source_alert_id=alert.id).one()
        decisions = (db.query(AlertReviewDecision)
                     .filter_by(alert_id=alert.id).all())
        assert result["training_eligible"] is True
        assert alert.status == "dismissed"
        assert image.eligible_for_training is True
        assert image.review_state == "approved"
        assert case.status == "resolved"
        assert len(decisions) == 3
        assert {decision.verdict for decision in decisions} == {
            "confirmed", "dismissed"}
        assert decisions[-1].classification == "independent_adjudication"
        assert db.query(GovernanceAuditLog).filter_by(
            action="review_disagreement.adjudicated").count() == 1


def test_adjudication_never_bypasses_pair_quality_quarantine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _primary, adjudicator, alert, case = _review_disagreement_fixture(
            db, quality_mode="quarantined")
        result = adjudicate_reviewer_disagreement(
            case.id,
            DisagreementAdjudicationIn(
                verdict="confirm", rationale="The person crossed the protected line."),
            db=db, user=adjudicator,
        )

        image = db.query(TrainingImage).filter_by(source_alert_id=alert.id).one()
        assert result["training_eligible"] is False
        assert image.eligible_for_training is False
        assert image.review_state == "quarantined"
        assert case.training_status == "adjudicated_quality_controlled"


def test_existing_reviewer_cannot_adjudicate_disagreement():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        primary, _adjudicator, _alert, case = _review_disagreement_fixture(db)
        with pytest.raises(HTTPException, match="independent of both reviewers"):
            adjudicate_reviewer_disagreement(
                case.id,
                DisagreementAdjudicationIn(
                    verdict="unclear", rationale="The evidence is incomplete."),
                db=db, user=primary,
            )


def test_missed_event_sample_always_requires_distinct_second_reviewer():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        annotator = User(email="annotator@example.test", password_hash="x", role="operator")
        verifier = User(email="verifier@example.test", password_hash="x", role="operator")
        dataset = Dataset(name="human_missed_events", classes_json=["intrusion"])
        db.add_all([annotator, verifier, dataset]); db.flush()
        case = AssuranceCase(
            dedup_key="missed:test:1", case_type="missed_event", severity="high",
            status="open", title="Missed intrusion",
            label_json={"label": "intrusion", "verified_by_user_id": annotator.id},
            training_status="labelled_sample_pending_independent_verification",
        )
        db.add(case); db.flush()
        image = TrainingImage(
            dataset_id=dataset.id, file_path="/evidence/missed.jpg", labeled=True,
            source_kind="human_missed_event", eligible_for_training=False,
            review_state="pending", source_extra={"assurance_case_id": case.id})
        db.add(image); db.flush()
        db.add(Annotation(image_id=image.id, class_label="intrusion",
                          bbox_json=[.5, .5, .2, .2], annotated_by=annotator.id,
                          verified=False))
        db.commit()

        with pytest.raises(HTTPException, match="independent second reviewer"):
            verify_missed_event_training(case.id, db=db, user=annotator)

        result = verify_missed_event_training(case.id, db=db, user=verifier)
        db.refresh(image)
        assert result["eligible_for_training"] is True
        assert image.eligible_for_training is True
        assert image.review_state == "approved"
