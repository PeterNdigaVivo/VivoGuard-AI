"""training_samples: source / shared / approval columns

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01

Adds the columns the upload + chain-training overhaul needs:

  source       'capture' | 'upload' | 'auto' — distinguishes images
                an operator uploaded from a phone/computer from those
                grabbed by the live-capture / auto-capture paths.
  shared       boolean default true — the sample joins the chain-
                wide pooled dataset (commit 2).
  approved     nullable boolean — quality gate (commit 3). NULL =
                pending review, true = approved for chain training,
                false = rejected.
  approved_by  user id who reviewed it.
  approved_at  timestamp of the review.

All columns nullable / defaulted so backfill is trivial — existing
rows become source='capture', shared=True, approved=NULL.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("training_samples",
        sa.Column("source", sa.String(16), nullable=False, server_default="capture"))
    op.add_column("training_samples",
        sa.Column("shared", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("training_samples",
        sa.Column("approved", sa.Boolean, nullable=True))
    op.add_column("training_samples",
        sa.Column("approved_by", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("training_samples",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("training_samples", "approved_at")
    op.drop_column("training_samples", "approved_by")
    op.drop_column("training_samples", "approved")
    op.drop_column("training_samples", "shared")
    op.drop_column("training_samples", "source")
