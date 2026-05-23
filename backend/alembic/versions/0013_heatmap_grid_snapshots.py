"""hourly heatmap grid snapshots for replay

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-23

Hourly snapshot of every heatmap layer (traffic / engagement /
congestion / paths) per camera. Drives the replay timeline in the
HeatmapPage so operators can scrub through a day or compare two
weekdays. Daily PNG archive (heatmap_snapshots, migration 0005)
stays — that's a different artefact.

Retention: 90 days, enforced by the celery task that writes new
rows. JSONB grid_data so the same table holds all four layers
without schema branching.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heatmap_grid_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer,
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("store_id", sa.Integer,
                  sa.ForeignKey("stores.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hour", sa.Integer, nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("grid_data", postgresql.JSONB, nullable=False),
        sa.Column("peak_value", sa.Float, nullable=True),
    )
    op.create_index(
        "ix_heatmap_grid_snap__cam_layer_time",
        "heatmap_grid_snapshots",
        ["camera_id", "layer", "captured_at"],
    )
    op.create_unique_constraint(
        "uq_heatmap_grid_snap__cam_layer_hour_day",
        "heatmap_grid_snapshots",
        ["camera_id", "layer", "captured_at"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_heatmap_grid_snap__cam_layer_hour_day",
                       "heatmap_grid_snapshots", type_="unique")
    op.drop_index("ix_heatmap_grid_snap__cam_layer_time",
                  table_name="heatmap_grid_snapshots")
    op.drop_table("heatmap_grid_snapshots")
