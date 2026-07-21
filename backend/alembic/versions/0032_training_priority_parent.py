"""training_jobs.priority + ai_models.parent_model_id

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-21

Part 2 of the training-acceleration work:
  - training_jobs.priority  — dispatch order (lower = sooner), default 5.
  - ai_models.parent_model_id — the model a fine-tune BUILDS on (incremental
    fine-tuning), self-referential FK, NULL for full retrains.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tj_cols = _columns(bind, "training_jobs")
    am_cols = _columns(bind, "ai_models")

    # training_jobs.priority — add only if missing.
    if "priority" not in tj_cols:
        op.add_column(
            "training_jobs",
            sa.Column("priority", sa.Integer(), nullable=False, server_default="5"),
        )

    # ai_models.parent_model_id — Q8 says this column may already exist on the
    # deployed DB; add it (and its self-FK) only when it's actually missing so
    # this migration is safe to run either way.
    if "parent_model_id" not in am_cols:
        op.add_column(
            "ai_models",
            sa.Column("parent_model_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_ai_models_parent_model_id",
            "ai_models", "ai_models",
            ["parent_model_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    am_cols = _columns(bind, "ai_models")
    if "parent_model_id" in am_cols:
        try:
            op.drop_constraint("fk_ai_models_parent_model_id", "ai_models",
                               type_="foreignkey")
        except Exception:
            pass
        op.drop_column("ai_models", "parent_model_id")
    if "priority" in _columns(bind, "training_jobs"):
        op.drop_column("training_jobs", "priority")
