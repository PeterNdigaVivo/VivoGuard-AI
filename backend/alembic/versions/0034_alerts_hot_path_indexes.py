"""alerts.event_id index + (status, created_at) composite

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-31

Perf Phase 1 (reporting/analytics optimization):
  - ix_alerts_event_id — every /alerts list call and the recorder's clip
    extraction join alerts -> detection_events on event_id; Postgres does
    not auto-index FK columns, so this join was a seq-scan candidate on
    the busiest table join in the API.
  - ix_alerts__status_created — the hot list/summary filter shape is
    "status = 'new' ORDER BY created_at DESC"; the composite lets one
    index satisfy filter + sort (the existing single-column indexes on
    status and created_at each cover only half).

Both guarded for idempotency (safe if a DBA added either by hand).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {ix["name"] for ix in _inspect(bind).get_indexes(table)}


def upgrade() -> None:
    existing = _index_names(op.get_bind(), "alerts")
    if "ix_alerts_event_id" not in existing:
        op.create_index("ix_alerts_event_id", "alerts", ["event_id"])
    if "ix_alerts__status_created" not in existing:
        op.create_index("ix_alerts__status_created", "alerts",
                        ["status", sa.text("created_at DESC")])


def downgrade() -> None:
    existing = _index_names(op.get_bind(), "alerts")
    if "ix_alerts__status_created" in existing:
        op.drop_index("ix_alerts__status_created", table_name="alerts")
    if "ix_alerts_event_id" in existing:
        op.drop_index("ix_alerts_event_id", table_name="alerts")
