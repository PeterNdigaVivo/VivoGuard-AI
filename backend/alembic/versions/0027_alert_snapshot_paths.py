"""alerts.snapshot_paths — checkout-dwell timeline snapshots

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-24

Nullable JSONB column holding the list of snapshot file paths
captured during a checkout-dwell session (one per minute from
counter-zone arrival to departure). Append-only:
  • Frames 1..N at alert-fire time (whatever already exists when
    the 8-min threshold trips).
  • Frames N+1..M appended atomically when the session closes.

24-hour retention from `alerts.created_at` via the new
alerting.prune_checkout_snapshots beat task — pruner deletes the
files and nulls the column. NULL for every non-checkout_dwell
alert and for snapshot-less checkout alerts (camera was offline
mid-session, etc.).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column("snapshot_paths", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "snapshot_paths")
