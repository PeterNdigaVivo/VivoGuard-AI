from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.training import (
    _wilson_lower_bound,
    independently_review_simulation_evidence,
    save_annotations,
    simulation_evidence_summary,
)
from app.config import settings
from app.database import Base
from app.models import (
    Annotation, AssuranceCase, Camera, Dataset, GovernanceAuditLog,
    TrainingImage, User,
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
        cases = db.query(AssuranceCase).order_by(AssuranceCase.id).all()
        assert len(cases) == 2
        assert all(case.training_status == "pending_primary_review" for case in cases)
        assert all(case.evidence["review_sla_minutes"] == 30 for case in cases)
        public_row = TrainingImageOut.model_validate(rows[0])
        assert public_row.evidence_source == "simulation_live_probe"
        assert public_row.synthetic is False
        assert not hasattr(public_row, "source_extra")
        # Keep both a missed-detection candidate and a detected-person control;
        # a one-sided sample cannot measure precision and recall.
        assert {row.camera_id for row in rows} == {cameras[0].id, cameras[1].id}
        assert {row.source_extra["probe_result"] for row in rows} == {
            "person_detected", "no_person_detected"}

        repeated = capture_live_probe_evidence(
            db,
            [_candidate(cameras[0].id, person=True, stamp=now),
             _candidate(cameras[1].id, person=False, stamp=now)],
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


def test_deduplicated_negative_pool_does_not_starve_person_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        cameras = [Camera(name=f"Camera {i}", brand="dahua",
                          connection_type="nvr_dahua", host="127.0.0.1")
                   for i in range(4)]
        db.add_all(cameras); db.commit()
        capture_live_probe_evidence(
            db,
            [_candidate(camera.id, person=False, stamp=now) for camera in cameras],
            run_id="negative-baseline", max_per_run=4, now=now,
        )

        result = capture_live_probe_evidence(
            db,
            [*[_candidate(camera.id, person=False, stamp=now) for camera in cameras],
             *[_candidate(camera.id, person=True, stamp=now) for camera in cameras]],
            run_id="control-follow-up", max_per_run=2, control_fraction=.5,
            now=now + timedelta(hours=1),
        )
        follow_up = db.query(TrainingImage).filter_by(
            simulation_run_id="control-follow-up").all()
        assert result == {"created": 2, "deduplicated": 4, "invalid": 0}
        assert len(follow_up) == 2
        assert all(row.source_extra["probe_result"] == "person_detected"
                   for row in follow_up)


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


def test_simulation_review_fails_closed_when_governance_case_is_missing(tmp_path, monkeypatch):
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
            run_id="missing-case-run", max_per_run=1, now=now,
        )
        image = db.query(TrainingImage).one()
        save_annotations(image.id, [], db=db, user=primary)
        db.query(AssuranceCase).delete(); db.commit()
        with pytest.raises(HTTPException, match="review case is missing"):
            independently_review_simulation_evidence(
                image.id,
                SimulationEvidenceReviewIn(
                    verdict="approve", rationale="No person is visible in the frame."),
                db=db, user=independent,
            )
        db.refresh(image)
        assert image.eligible_for_training is False


def test_simulation_summary_uses_reviewed_confusion_matrix_and_conservative_gate():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        dataset = Dataset(name="feedback-person", classes_json=["person"])
        camera = Camera(name="Camera", brand="dahua",
                        connection_type="nvr_dahua", host="127.0.0.1")
        db.add_all([dataset, camera]); db.flush()
        outcomes = [
            ("person_detected", "person_present"),
            ("person_detected", "no_person_present"),
            ("no_person_detected", "person_present"),
            ("no_person_detected", "no_person_present"),
        ]
        for index, (probe_result, primary_outcome) in enumerate(outcomes):
            db.add(TrainingImage(
                dataset_id=dataset.id,
                camera_id=camera.id,
                file_path=f"reviewed-{index}.jpg",
                labeled=True,
                source_kind="human_verified_simulation",
                eligible_for_training=True,
                review_state="approved",
                simulation_run_id=f"run-{index}",
                source_extra={
                    "source": "simulation_live_probe",
                    "synthetic": False,
                    "probe_result": probe_result,
                    "primary_review_outcome": primary_outcome,
                    "model_name": "yolov8n.pt",
                },
            ))
        db.add(AssuranceCase(
            dedup_key="simulation-evidence:overdue",
            case_type="simulation_evidence_review",
            severity="medium",
            status="open",
            camera_id=camera.id,
            title="Overdue simulation review",
            human_review_required=True,
            first_seen_at=now - timedelta(minutes=31),
            last_seen_at=now - timedelta(minutes=31),
        ))
        db.commit()

        summary = simulation_evidence_summary(db=db, _u=object())
        assert summary["confusion_matrix"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
        assert summary["precision"] == .5
        assert summary["recall"] == .5
        assert summary["claimable_99"] is False
        assert summary["camera_slices_total"] == 1
        assert summary["camera_slices_proven_99"] == 0
        assert summary["overdue"] == 1
        assert summary["model_sample_counts"] == {"yolov8n.pt": 4}


def test_wilson_gate_rejects_small_perfect_samples():
    assert _wilson_lower_bound(1, 1) < .99
    assert _wilson_lower_bound(100, 100) < .99
    assert _wilson_lower_bound(1000, 1000) > .99
