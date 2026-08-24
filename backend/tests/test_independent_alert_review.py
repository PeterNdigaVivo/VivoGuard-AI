from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Alert, AlertReviewDecision, AssuranceCase, Camera, Dataset,
    DetectionEvent, TrainingImage, User,
)
from app.services.alert_feedback import record_independent_verdict


@pytest.fixture()
def review_case():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    primary = User(email="primary@vivo", password_hash="x", role="operator")
    independent = User(
        email="independent@vivo", password_hash="x", role="operator",
    )
    camera = Camera(
        name="Review Camera", brand="dahua", connection_type="nvr_dahua",
        host="127.0.0.1",
    )
    dataset = Dataset(name="feedback-intrusion", classes_json=["intrusion"])
    db.add_all([primary, independent, camera, dataset])
    db.flush()
    event = DetectionEvent(
        camera_id=camera.id, detection_type="intrusion", confidence=.9,
        bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
    )
    db.add(event)
    db.flush()
    alert = Alert(event_id=event.id, status="confirmed")
    db.add(alert)
    db.flush()
    db.add_all([
        AlertReviewDecision(
            alert_id=alert.id, reviewer_id=primary.id, verdict="confirmed",
        ),
        TrainingImage(
            dataset_id=dataset.id, camera_id=camera.id,
            file_path="/evidence/frame.jpg", labeled=True,
            source_alert_id=alert.id, source_kind="operator_confirmed",
            eligible_for_training=False, review_state="pending",
        ),
    ])
    db.commit()
    yield db, alert, independent
    db.close()


def test_independent_agreement_promotes_quarantined_training_evidence(review_case):
    db, alert, independent = review_case
    result = record_independent_verdict(
        db, alert.id, "confirm", independent,
    )

    image = db.query(TrainingImage).filter_by(source_alert_id=alert.id).one()
    assert result["agreed"] is True
    assert result["training_eligible"] is True
    assert image.eligible_for_training is True
    assert image.review_state == "approved"
    assert image.source_extra["independent_review_agreed"] is True
    assert db.query(AlertReviewDecision).count() == 2
    assert db.query(AssuranceCase).count() == 0


def test_independent_disagreement_quarantines_and_opens_case(review_case):
    db, alert, independent = review_case
    result = record_independent_verdict(
        db, alert.id, "dismiss", independent,
    )

    db.refresh(alert)
    image = db.query(TrainingImage).filter_by(source_alert_id=alert.id).one()
    case = db.query(AssuranceCase).one()
    assert result["agreed"] is False
    assert result["training_eligible"] is False
    assert image.eligible_for_training is False
    assert image.review_state == "quarantined"
    assert alert.review_only is True
    assert alert.training_eligible is False
    assert case.case_type == "reviewer_disagreement"
    assert case.training_status == "blocked_pending_adjudication"


def test_primary_reviewer_cannot_review_their_own_alert(review_case):
    db, alert, _independent = review_case
    primary = db.query(User).filter_by(email="primary@vivo").one()

    with pytest.raises(Exception, match="reviewer must be independent"):
        record_independent_verdict(db, alert.id, "confirm", primary)
