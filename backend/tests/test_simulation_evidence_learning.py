from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.training import (
    independently_review_simulation_evidence,
    save_annotations,
)
from app.config import settings
from app.database import Base
from app.models import (
    Annotation, AssuranceCase, Camera, GovernanceAuditLog, TrainingImage, User,
)
from app.schemas.training import (
    AnnotationIn, SimulationEvidenceReviewIn, TrainingImageOut,
)
from app.simulation.evidence import LiveProbeCandidate, capture_live_probe_evidence


def _candidate(camera_id: int, *, person: bool, stamp: datetime) -> LiveProbeCandidate:
    detections = ([{"cls": "person", "conf": .91,
                    "bbox_norm": [.1, .2, .5, .9]}] if person else [])
    return LiveProbeCandidate(camera_id=camera_id, jpeg=b"real-jpeg-bytes",
                              detections=detections, captured_at=stamp)


def test_live_probe_capture_is_bounded_deduplicated_and_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        cameras = [Camera(name=f"Camera {i}", brand="dahua",
                          connection_type="nvr_dahua", host="127.0.0.1")
                   for i in range(3)]
        db.add_all(cameras); db.commit()
        result = capture_live_probe_evidence(
            db,
            [_candidate(cameras[0].id, person=True, stamp=now),
             _candidate(cameras[1].id, person=False, stamp=now),
             _candidate(cameras[2].id, person=False, stamp=now)],
            run_id="run-one", max_per_run=2, now=now,
        )
        assert result["created"] == 2
        rows = db.query(TrainingImage).order_by(TrainingImage.id).all()
        assert len(rows) == 2
        assert all(row.source_kind == "simulation" for row in rows)
        assert all(row.eligible_for_training is False for row in rows)
        assert all(row.review_state == "quarantined" for row in rows)
        assert all(row.source_extra["synthetic"] is False for row in rows)
        public_row = TrainingImageOut.model_validate(rows[0])
        assert public_row.evidence_source == "simulation_live_probe"
        assert public_row.synthetic is False
        assert not hasattr(public_row, "source_extra")
        # No-person candidates are deliberately prioritised for missed-event review.
        assert {row.camera_id for row in rows} == {cameras[1].id, cameras[2].id}

        repeated = capture_live_probe_evidence(
            db,
            [_candidate(cameras[1].id, person=False, stamp=now),
             _candidate(cameras[2].id, person=False, stamp=now)],
            run_id="run-two", max_per_run=2, now=now + timedelta(hours=1),
        )
        assert repeated == {"created": 0, "deduplicated": 2, "invalid": 0}
        assert db.query(TrainingImage).count() == 2


def test_simulation_evidence_requires_distinct_second_reviewer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        primary = User(email="primary@example.test", password_hash="x", role="operator")
        independent = User(email="independent@example.test", password_hash="x", role="operator")
        camera = Camera(name="Camera", brand="dahua",
                        connection_type="nvr_dahua", host="127.0.0.1")
        db.add_all([primary, independent, camera]); db.commit()
        capture_live_probe_evidence(
            db, [_candidate(camera.id, person=True, stamp=now)],
            run_id="review-run", max_per_run=1, now=now,
        )
        image = db.query(TrainingImage).one()

        saved = save_annotations(
            image.id,
            [AnnotationIn(class_label="person", bbox_json=[.3, .55, .4, .7],
                          verified=True, auto_suggested=False)],
            db=db, user=primary,
        )
        assert len(saved) == 1
        db.refresh(image)
        assert image.eligible_for_training is False
        assert image.review_state == "pending_independent_review"
        assert db.query(Annotation).one().verified is False
        case = db.query(AssuranceCase).filter_by(
            case_type="simulation_evidence_review").one()
        assert case.status == "open"

        body = SimulationEvidenceReviewIn(
            verdict="approve", rationale="The real frame and corrected box agree.")
        with pytest.raises(HTTPException, match="independent second reviewer"):
            independently_review_simulation_evidence(
                image.id, body, db=db, user=primary)

        result = independently_review_simulation_evidence(
            image.id, body, db=db, user=independent)
        db.refresh(image); db.refresh(case)
        assert result["eligible_for_training"] is True
        assert image.source_kind == "human_verified_simulation"
        assert image.review_state == "approved"
        assert db.query(Annotation).one().verified is True
        assert case.status == "resolved"
        assert db.query(GovernanceAuditLog).filter_by(
            action="simulation_evidence.approved").count() == 1


def test_reviewed_no_person_frame_can_be_approved_as_hard_negative(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        primary = User(email="primary@example.test", password_hash="x", role="operator")
        independent = User(email="independent@example.test", password_hash="x", role="operator")
        camera = Camera(name="Camera", brand="dahua",
                        connection_type="nvr_dahua", host="127.0.0.1")
        db.add_all([primary, independent, camera]); db.commit()
        capture_live_probe_evidence(
            db, [_candidate(camera.id, person=False, stamp=now)],
            run_id="negative-run", max_per_run=1, now=now,
        )
        image = db.query(TrainingImage).one()
        save_annotations(image.id, [], db=db, user=primary)
        result = independently_review_simulation_evidence(
            image.id,
            SimulationEvidenceReviewIn(
                verdict="approve", rationale="No person is visible in this real frame."),
            db=db, user=independent,
        )
        db.refresh(image)
        assert result["eligible_for_training"] is True
        assert image.labeled is True
        assert db.query(Annotation).count() == 0
