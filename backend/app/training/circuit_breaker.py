"""Dataset training circuit-breaker policy.

The breaker protects a *dataset revision* from repeatedly consuming worker
capacity after deterministic data-preparation failures.  Operational failures
(worker restarts, broker starvation, stalls) are deliberately not attributed
to the dataset, and breaker refusals never count as fresh failures.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session


MAX_DATASET_FAILURES = 3
SUSPENDED_MARKER = "[suspended]"
_PRIOR_DESCRIPTION = "Prior description: "


def is_dataset_caused_failure(message: str | None) -> bool:
    """True only for failures that a changed/fixed dataset can resolve."""
    text = (message or "").strip().lower()
    return text.startswith((
        "dataset prep:",
        "dataset sanitize:",
        "dataset directory missing:",
        "train.txt has 0 image paths",
        "dataset prep produced no train.txt",
        "first training image not found on disk:",
        "insufficient data:",
    ))


def is_breaker_refusal(message: str | None) -> bool:
    text = (message or "").lower()
    return "auto-suspended" in text or text.startswith("dataset is suspended")


def count_failures_for_revision(
    failures: list[tuple[datetime | None, str | None]],
    *,
    revision_at: datetime | None,
    last_success_at: datetime | None,
) -> int:
    """Count dataset-caused failures since data or a successful run changed.

    Keeping this calculation pure makes the distinction between data and
    infrastructure failures regression-testable without a production DB.
    """
    def aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    cutoffs = [aware(value) for value in (revision_at, last_success_at) if value]
    cutoff = max(cutoffs) if cutoffs else None
    return sum(
        1 for created_at, message in failures
        if is_dataset_caused_failure(message)
        and not is_breaker_refusal(message)
        and (cutoff is None or created_at is None or aware(created_at) >= cutoff)
    )


def suspension_description(description: str | None, failures: int) -> str:
    """Stamp an idempotent suspension marker while preserving prior text."""
    if (description or "").startswith(SUSPENDED_MARKER):
        return description or SUSPENDED_MARKER
    prior = description or "(none)"
    return (
        f"{SUSPENDED_MARKER} {failures} dataset-caused training failures on "
        f"the current data revision. Admin review required. "
        f"{_PRIOR_DESCRIPTION}{prior}"
    )


def restore_description(description: str | None) -> str | None:
    """Restore text preserved by :func:`suspension_description`."""
    text = description or ""
    if not text.startswith(SUSPENDED_MARKER):
        return description
    if _PRIOR_DESCRIPTION not in text:
        return None
    prior = text.split(_PRIOR_DESCRIPTION, 1)[1]
    return None if prior == "(none)" else prior


def has_admin_override(job) -> bool:
    return bool((job.config_json or {}).get("circuit_breaker_override"))


def dataset_circuit_state(db: Session, dataset) -> dict:
    """Return the current revision's breaker state without dispatching work."""
    from app.models import Annotation, TrainingImage, TrainingJob

    already_suspended = bool(
        dataset and (dataset.description or "").startswith(SUSPENDED_MARKER))
    if dataset is None:
        return {"suspended": False, "failures": 0, "reason": None}

    image_revision = db.query(func.max(TrainingImage.created_at)).filter(
        TrainingImage.dataset_id == dataset.id).scalar()
    annotation_revision = (db.query(func.max(Annotation.created_at))
                           .join(TrainingImage, TrainingImage.id == Annotation.image_id)
                           .filter(TrainingImage.dataset_id == dataset.id).scalar())
    revisions = [value for value in (image_revision, annotation_revision) if value]
    revision_at = max(revisions) if revisions else None

    last_success_at = (db.query(func.max(TrainingJob.completed_at))
                       .filter(TrainingJob.dataset_id == dataset.id,
                               TrainingJob.status == "done").scalar())
    failed_rows = (db.query(TrainingJob.created_at, TrainingJob.error_message)
                   .filter(TrainingJob.dataset_id == dataset.id,
                           TrainingJob.status == "failed").all())
    failures = count_failures_for_revision(
        list(failed_rows), revision_at=revision_at,
        last_success_at=last_success_at)
    suspended = already_suspended or failures >= MAX_DATASET_FAILURES
    reason = None
    if suspended:
        reason = (f"Dataset is suspended after {failures} dataset-caused "
                  "training failures on its current revision. An admin may "
                  "force one reviewed retry; successful completion clears "
                  "the suspension.")
    return {"suspended": suspended, "failures": failures, "reason": reason}


def enforce_dataset_circuit(db: Session, job) -> str | None:
    """Stamp a newly-tripped breaker and return its refusal reason."""
    if has_admin_override(job):
        return None
    from app.models import Dataset

    dataset = db.get(Dataset, job.dataset_id) if job.dataset_id else None
    state = dataset_circuit_state(db, dataset)
    if not state["suspended"]:
        return None
    if dataset is not None:
        dataset.description = suspension_description(
            dataset.description, state["failures"])
    return state["reason"]
