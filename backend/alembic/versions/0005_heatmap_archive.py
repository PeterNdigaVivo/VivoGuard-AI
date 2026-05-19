"""heatmap snapshots — daily PNG archive

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heatmap_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("day", sa.Date, nullable=False, index=True),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("peak_value", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("camera_id", "day", name="uq_heatmap_snapshot_cam_day"),
    )


def downgrade() -> None:
    op.drop_table("heatmap_snapshots")
