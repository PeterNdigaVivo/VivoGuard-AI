from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.operations import (
    RecallSampleBatchIn, RecallSampleReviewIn, generate_recall_samples,
    review_recall_sample,
)
from app.database import Base
from app.models import (
    Alert, AssuranceCase, Camera, DetectionEvent, RecordingClip, Store, User,
)
from app.services.alert_quality import quality_scorecards
from app.tasks.operations_assurance import extract_recall_sample


def _base(db: Session):
    store = Store(name="Recall Store", country="Kenya")
    users = [
        User(email=f"reviewer-{index}@example.test", password_hash="x",
             role="operator") for index in range(1, 4)
    ]
    camera = Camera(name="Recall Camera", site="Recall Store", brand="dahua",
                    connection_type="nvr_dahua", host="127.0.0.1",
                    status="online", ai_enabled=True)
    db.add_all([store, *users]); db.flush()
    camera.store_id = store.id
    db.add(camera); db.flush()
    return store, camera, users


def _sample(db: Session, camera: Camera, *, started: datetime,
            status: str = "pending_primary_review") -> AssuranceCase:
    case = AssuranceCase(
        dedup_key=f"recall-test:{camera.id}:{started.isoformat()}",
        case_type="recall_sample", severity="medium", status=status,
        store_id=camera.store_id, camera_id=camera.id,
        title="Blind random-footage recall sample",
        evidence={"extraction_status": "ready",
                  "sample_started_at": started.isoformat(),
                  "duration_seconds": 30},
        training_status="not_training_evidence_pending_review",
    )
    db.add(case); db.commit()
    return case


def test_generate_recall_batch_is_reproducible_and_queues_extraction(
    tmp_path, monkeypatch,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store, camera, users = _base(db)
        recording = tmp_path / "recording.mp4"
        recording.write_bytes(b"retained-video-placeholder")
        started = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.add(RecordingClip(
            camera_id=camera.id, store_id=store.id, window_id="test",
            file_path=str(recording), started_at=started,
            ended_at=started + timedelta(minutes=5), status="completed",
        ))
        db.commit()
        queued: list[int] = []
        monkeypatch.setattr(
            "app.tasks.operations_assurance.extract_recall_sample.delay",
            lambda case_id: queued.append(case_id),
        )

        result = generate_recall_samples(
            RecallSampleBatchIn(sample_count=1, duration_seconds=30,
                                seed="repeatable-campaign"),
            db=db, user=users[0],
        )

        case = db.get(AssuranceCase, result["case_ids"][0])
        assert result["created"] == 1
        assert queued == [case.id]
        assert case.evidence["seed_hash"] == result["seed_hash"]
        assert "recording.mp4" not in str(case.evidence)
        assert case.status == "pending_evidence"

        repeated = generate_recall_samples(
            RecallSampleBatchIn(sample_count=1, duration_seconds=30,
                                seed="repeatable-campaign"),
            db=db, user=users[0],
        )
        assert repeated["created"] == 0
        assert repeated["reused"] == 1
        assert repeated["case_ids"] == [case.id]


def test_double_reviewed_random_sample_matches_existing_alert():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store, camera, users = _base(db)
        started = datetime.now(timezone.utc) - timedelta(minutes=2)
        case = _sample(db, camera, started=started)
        event = DetectionEvent(
            camera_id=camera.id, detection_type="intrusion", confidence=.9,
            bbox_json=[0, 0, 1, 1], timestamp=started + timedelta(seconds=10),
        )
        db.add(event); db.flush(); db.add(Alert(event_id=event.id)); db.commit()
        body = RecallSampleReviewIn(
            outcome="target_event", event_label="intrusion",
            rationale="A person crosses the protected stockroom boundary.",
        )

        first = review_recall_sample(case.id, body, db=db, user=users[0])
        second = review_recall_sample(case.id, body, db=db, user=users[1])

        assert first["result"] == "pending_independent_review"
        assert second["result"] == "recall_true_positive"
        assert case.status == "resolved"
        assert case.evidence["matched_alert_id"] is not None
        assert db.query(AssuranceCase).filter_by(case_type="missed_event").count() == 0


def test_recall_extraction_writes_only_to_bounded_review_directory(
    tmp_path, monkeypatch,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source = recordings / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr("app.tasks.operations_assurance.settings.recordings_dir",
                        str(recordings))
    with Session(engine) as db:
        store, camera, _users = _base(db)
        clip = RecordingClip(
            camera_id=camera.id, store_id=store.id, window_id="extract",
            file_path=str(source), started_at=datetime.now(timezone.utc),
            status="recording",
        )
        db.add(clip); db.flush()
        case = AssuranceCase(
            dedup_key="recall-extract", case_type="recall_sample",
            severity="medium", status="pending_evidence", store_id=store.id,
            camera_id=camera.id, title="Recall sample",
            evidence={"recording_clip_id": clip.id, "offset_seconds": 5,
                      "duration_seconds": 30, "extraction_status": "queued"},
        )
        db.add(case); db.commit(); case_id = case.id

    def fake_run(command, **_kwargs):
        output = command[-1]
        from pathlib import Path
        Path(output).write_bytes(b"bounded-sample")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.tasks.operations_assurance.subprocess.run", fake_run)
    monkeypatch.setattr("app.tasks.operations_assurance.SessionLocal",
                        lambda: Session(engine))
    extract_recall_sample.run(case_id)

    with Session(engine) as db:
        case = db.get(AssuranceCase, case_id)
        assert case.evidence["extraction_status"] == "ready"
        assert case.status == "pending_primary_review"
    assert (recordings / "recall_samples" / f"{case_id}.mp4").read_bytes() == b"bounded-sample"


def test_verified_random_sample_creates_missed_event_without_training():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store, camera, users = _base(db)
        case = _sample(db, camera, started=datetime.now(timezone.utc))
        body = RecallSampleReviewIn(
            outcome="target_event", event_label="fall",
            rationale="A person falls and remains on the floor in view.",
        )
        review_recall_sample(case.id, body, db=db, user=users[0])
        result = review_recall_sample(case.id, body, db=db, user=users[1])

        missed = db.query(AssuranceCase).filter_by(case_type="missed_event").one()
        assert result["result"] == "recall_false_negative"
        assert result["training_eligible"] is False
        assert missed.training_status == "blocked_pending_visual_annotation"
        assert missed.label_json["independently_verified"] is True


def test_recall_disagreement_requires_third_distinct_reviewer():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store, camera, users = _base(db)
        case = _sample(db, camera, started=datetime.now(timezone.utc))
        target = RecallSampleReviewIn(
            outcome="target_event", event_label="intrusion",
            rationale="Movement crosses the protected boundary in the clip.",
        )
        empty = RecallSampleReviewIn(
            outcome="no_target_event",
            rationale="Only ordinary customer passage is visible in the clip.",
        )
        review_recall_sample(case.id, target, db=db, user=users[0])
        disagreement = review_recall_sample(case.id, empty, db=db, user=users[1])
        with pytest.raises(HTTPException, match="distinct reviewer"):
            review_recall_sample(case.id, empty, db=db, user=users[1])
        final = review_recall_sample(case.id, empty, db=db, user=users[2])

        assert disagreement["result"] == "reviewer_disagreement"
        assert final["result"] == "recall_no_target_event"
        assert len(case.label_json["reviews"]) == 3


def test_scorecard_recall_uses_only_independent_random_footage_events():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store, camera, _users = _base(db)
        now = datetime.now(timezone.utc)
        for suffix, root_cause in (("tp", "recall_true_positive"),
                                   ("fn", "recall_false_negative")):
            db.add(AssuranceCase(
                dedup_key=f"recall-score:{suffix}", case_type="recall_sample",
                severity="medium", status="resolved", store_id=store.id,
                camera_id=camera.id, title="Recall evidence",
                root_cause=root_cause, reviewed_at=now, resolved_at=now,
                label_json={"final_outcome": "target_event",
                            "final_event_label": "intrusion"},
            ))
        db.commit()

        card = quality_scorecards(db)[0]
        assert card["detection_type"] == "intrusion"
        assert card["recall"] == .5
        assert card["recall_lower_bound_95"] < card["recall"]
        assert card["target_99_recall_evidence_met"] is False
        assert card["target_99_evidence_met"] is False
        assert card["recall_true_positive_events"] == 1
        assert card["recall_false_negative_events"] == 1
