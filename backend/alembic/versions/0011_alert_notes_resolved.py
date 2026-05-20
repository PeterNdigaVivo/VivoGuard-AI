"""alerts.notes + alerts.resolved_at

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-20

Two additions for the May-2026 alert-feed redesign:

  notes        Investigation notes operators add via the "📋 Add Note"
               button. Free-text, optional, accumulates over time.
  resolved_at  Timestamp set when the alert is marked Resolved (✅).
               Distinct from `acknowledged_at` (also bumped on
               dismiss/confirm) — `resolved_at` only fires on the
               explicit Resolved action so it can be reported on.

Both nullable to keep this migration non-breaking for existing rows.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("notes", sa.Text, nullable=True))
    op.add_column("alerts",
                  sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("alerts", "resolved_at")
    op.drop_column("alerts", "notes")
