"""training_images.preview_path + training_samples.preview_path

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-23

Adds the second on-disk path for each training row. file_path /
frame_path stay the CLEAN crop (the only thing YOLO ever sees —
drawing a box on it would teach the model to detect orange boxes,
not uniforms). preview_path is the same crop with an orange
bounding box overlay, surfaced in any future operator-review UI.

Both columns are nullable so existing rows are unaffected; preview
generation is best-effort at write-time and a failure must never
block the clean save or DB commit.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "training_images",
        sa.Column("preview_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "training_samples",
        sa.Column("preview_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("training_samples", "preview_path")
    op.drop_column("training_images",  "preview_path")
