"""training_samples — per-frame classification labels

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-24

One row per captured + labelled frame for CLASSIFICATION detectors
(shutter / door status). Frame → label, no bounding boxes. Backs the
/training/shutter collection UI.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "training_samples",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("detector_type", sa.String(32), nullable=False, index=True),
        sa.Column("label", sa.String(32), nullable=False, index=True),
        sa.Column("camera_id", sa.Integer,
                  sa.ForeignKey("cameras.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("store_id", sa.Integer,
                  sa.ForeignKey("stores.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("frame_path", sa.Text, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("labeled_by", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
    )
    op.create_index(
        "ix_training_samples__detector_camera_label",
        "training_samples",
        ["detector_type", "camera_id", "label"],
    )


def downgrade() -> None:
    op.drop_index("ix_training_samples__detector_camera_label",
                  table_name="training_samples")
    op.drop_table("training_samples")
