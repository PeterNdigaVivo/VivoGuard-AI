"""recording_clips — rolling recording window ledger

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-14

One row per camera per recording window written by the recorder service.
Rows are kept (status='deleted', file_path NULL) after the window's files
are purged, as an audit trail.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recording_clips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_id", sa.Integer(),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("window_id", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_size_mb", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="recording"),
    )
    op.create_index("ix_recording_clips_camera_id", "recording_clips", ["camera_id"])
    op.create_index("ix_recording_clips_window_id", "recording_clips", ["window_id"])
    op.create_index("ix_recording_clips_camera_started", "recording_clips",
                    ["camera_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_recording_clips_camera_started", table_name="recording_clips")
    op.drop_index("ix_recording_clips_window_id", table_name="recording_clips")
    op.drop_index("ix_recording_clips_camera_id", table_name="recording_clips")
    op.drop_table("recording_clips")
