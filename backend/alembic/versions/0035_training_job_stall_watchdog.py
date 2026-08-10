"""training_jobs stall-watchdog columns

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-10

Cross-store job 606 sat `queued` while a crashed job blocked the
2-running cap for 12h (job 235). The dispatcher now runs a 90-min
no-epoch-progress watchdog; it needs:
  - last_progress_at — heartbeat the trainer bumps at job start and
    after every completed epoch.
  - celery_task_id — captured at dispatch so the watchdog can revoke
    (terminate) the stuck Celery task before requeueing the job.

Both guarded for idempotency.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def upgrade() -> None:
    existing = _column_names(op.get_bind(), "training_jobs")
    if "last_progress_at" not in existing:
        op.add_column("training_jobs",
                      sa.Column("last_progress_at",
                                sa.DateTime(timezone=True), nullable=True))
    if "celery_task_id" not in existing:
        op.add_column("training_jobs",
                      sa.Column("celery_task_id", sa.String(64),
                                nullable=True))


def downgrade() -> None:
    existing = _column_names(op.get_bind(), "training_jobs")
    if "celery_task_id" in existing:
        op.drop_column("training_jobs", "celery_task_id")
    if "last_progress_at" in existing:
        op.drop_column("training_jobs", "last_progress_at")
