"""hot-path indexes: detection_type/time + metric_type per camera/zone

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-19

Adds covering indexes for query patterns the schema audit found
unserved by existing indexes:

  * detection_events (detection_type, timestamp DESC)
      Fleet-wide "by detection_type, recent" reads (alert engine /
      shop-open checks) that do NOT pin camera_id. The existing
      ix_detection_events__camera_type_ts (0012) leads with
      camera_id and can't serve a camera-agnostic scan.

  * metric_snapshots (metric_type, camera_id, period_start DESC)
      Per-camera dashboard reads. Existing indexes lead with
      store_id or camera_id alone; neither serves metric_type+camera.

  * metric_snapshots (metric_type, zone_id, period_start DESC)
      Per-zone checkout/queue reads. zone_id was previously
      unindexed entirely.

All use IF NOT EXISTS so re-running on a hand-indexed DB is safe —
matches the 0012 convention. Naming: ix_<table>__<cols-shortened>.

Production note: on the large detection_events / metric_snapshots
tables a plain CREATE INDEX takes an ACCESS EXCLUSIVE lock. If the
tables are big enough to matter, run these as CREATE INDEX
CONCURRENTLY by hand in a low-traffic window instead and stamp the
revision with `alembic stamp 0023`. The file uses the standard
in-transaction create to match house style.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_detection_events__type_ts",
        "detection_events",
        ["detection_type", sa.text("timestamp DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_metric_snapshots__metric_camera_period",
        "metric_snapshots",
        ["metric_type", "camera_id", sa.text("period_start DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_metric_snapshots__metric_zone_period",
        "metric_snapshots",
        ["metric_type", "zone_id", sa.text("period_start DESC")],
        if_not_exists=True,
    )


def downgrade() -> None:
    for name in (
        "ix_detection_events__type_ts",
        "ix_metric_snapshots__metric_camera_period",
        "ix_metric_snapshots__metric_zone_period",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
