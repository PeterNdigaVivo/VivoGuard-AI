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


def upgrade() -> None:
    op.add_column(
        "training_jobs",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="5"),
    )
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
    op.drop_constraint("fk_ai_models_parent_model_id", "ai_models",
                       type_="foreignkey")
    op.drop_column("ai_models", "parent_model_id")
    op.drop_column("training_jobs", "priority")
