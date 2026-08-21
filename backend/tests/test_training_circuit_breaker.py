"""Regression tests for dataset-vs-infrastructure training failures."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Dataset, TrainingJob
from app.training.circuit_breaker import (
    count_failures_for_revision, has_admin_override,
    is_breaker_refusal, is_dataset_caused_failure,
    restore_description, suspension_description,
)


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def test_breaker_refusals_do_not_increase_failure_count():
    rows = [
        (NOW, "dataset prep: insufficient validation images"),
        (NOW, "Dataset has 33 prior failures (>= 3) — auto-suspended."),
        (NOW, "Dataset has 34 prior failures (>= 3) — auto-suspended."),
        (NOW, "Dataset has 35 prior failures (>= 3) — auto-suspended."),
    ]
    assert count_failures_for_revision(
        rows, revision_at=NOW - timedelta(days=1), last_success_at=None) == 1
    assert is_breaker_refusal(rows[-1][1])


def test_worker_restarts_and_stalls_are_not_dataset_failures():
    operational = [
        "auto-failed: worker restarted while job was running; already requeued 2 time(s)",
        "auto-failed: trainer never started within 60s; already requeued 2 time(s)",
        "auto-failed: no epoch progress for > 90 min; already requeued 2 time(s)",
    ]
    assert not any(is_dataset_caused_failure(message) for message in operational)
    assert count_failures_for_revision(
        [(NOW, message) for message in operational],
        revision_at=None, last_success_at=None) == 0


def test_only_current_dataset_revision_counts():
    rows = [
        (NOW - timedelta(days=2), "dataset prep: corrupt manifest"),
        (NOW + timedelta(minutes=1), "dataset sanitize: unreadable image"),
    ]
    assert count_failures_for_revision(
        rows, revision_at=NOW, last_success_at=None) == 1


def test_success_resets_earlier_dataset_failures():
    rows = [
        (NOW - timedelta(hours=2), "insufficient data: only 12 images"),
        (NOW + timedelta(hours=2), "train.txt has 0 image paths — no labels"),
    ]
    assert count_failures_for_revision(
        rows, revision_at=None, last_success_at=NOW) == 1


def test_suspension_description_is_idempotent_and_recoverable():
    stamped = suspension_description("confirmed feedback", 3)
    assert suspension_description(stamped, 99) == stamped
    assert restore_description(stamped) == "confirmed feedback"
    assert restore_description(suspension_description(None, 3)) is None


def test_only_explicit_job_override_bypasses_breaker():
    assert not has_admin_override(SimpleNamespace(config_json={}))
    assert has_admin_override(SimpleNamespace(
        config_json={"circuit_breaker_override": True}))


def test_direct_celery_delivery_cannot_start_suspended_dataset(monkeypatch):
    """Direct .delay paths must obey the same guard as the dispatcher."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        dataset = Dataset(name="feedback-crowd",
                          description="[suspended] reviewed root cause pending",
                          classes_json=["crowd"])
        db.add(dataset); db.flush()
        job = TrainingJob(model_name="yolov8n", dataset_id=dataset.id,
                          config_json={}, status="queued",
                          current_epoch=5, total_epochs=50)
        db.add(job); db.commit()
        job_id = job.id

    import app.database
    import app.training.trainer
    from app.tasks.training import run_training_job

    monkeypatch.setattr(app.database, "SessionLocal", sessions)
    trainer_called = False

    def forbidden(_job_id):
        nonlocal trainer_called
        trainer_called = True

    monkeypatch.setattr(app.training.trainer, "run_job", forbidden)
    run_training_job.run(job_id)

    with sessions() as db:
        result = db.get(TrainingJob, job_id)
        assert result.status == "cancelled"
        assert result.current_epoch == 5
        assert "Dataset is suspended" in result.error_message
    assert trainer_called is False
