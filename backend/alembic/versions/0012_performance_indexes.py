"""performance indexes for 117-camera scale

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-21

At 117 cameras × 25 stores the dashboard endpoints went from
~150ms to 2-4 seconds. Most queries are time-windowed aggregates
filtered by store_id or (camera_id, detection_type). Postgres was
falling back to sequential scans on the big tables; the indexes
below cover the hot paths the analytics + alerts + heatmap
endpoints share.

All indexes use IF NOT EXISTS so re-running the migration on a
hand-indexed db is safe.

Naming convention:
  ix_<table>__<cols-shortened>     for compound indexes
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `op.create_index` with `postgresql_using='btree'` is the default
    # for sqlalchemy. The `if_not_exists` parameter is supported in
    # alembic 1.13+ which is what we ship.
    op.create_index(
        "ix_metric_snapshots__store_metric_period",
        "metric_snapshots",
        ["store_id", "metric_type", sa.text("period_start DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_metric_snapshots__camera_period",
        "metric_snapshots",
        ["camera_id", sa.text("period_start DESC")],
        if_not_exists=True,
    )
    # detection_events has `timestamp` (not `created_at`) and no
    # `store_id` column — filtering by store goes through cameras.
    # Spec asked for (store_id, created_at) and (camera_id,
    # detection_type, created_at); we substitute the existing
    # columns. The first is already covered by the model's
    # camera_id+timestamp index; we add the type-specific one.
    op.create_index(
        "ix_detection_events__camera_type_ts",
        "detection_events",
        ["camera_id", "detection_type", sa.text("timestamp DESC")],
        if_not_exists=True,
    )
    # alerts has no store_id column either — feed scope joins
    # through detection_events.camera_id. Index covers the very
    # common (status, created_at DESC) read for the live feed.
    op.create_index(
        "ix_alerts__status_created",
        "alerts",
        ["status", sa.text("created_at DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_cameras__store_ai_enabled",
        "cameras",
        ["store_id", "ai_enabled"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_zones__camera",
        "zones",
        ["camera_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    for name in (
        "ix_metric_snapshots__store_metric_period",
        "ix_metric_snapshots__camera_period",
        "ix_detection_events__camera_type_ts",
        "ix_alerts__status_created",
        "ix_cameras__store_ai_enabled",
        "ix_zones__camera",
    ):
        op.execute(f'DROP INDEX IF EXISTS {name}')
