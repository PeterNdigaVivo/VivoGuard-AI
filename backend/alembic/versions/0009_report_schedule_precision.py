"""cron-precise scheduled reports

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-19

The dispatcher used to fire any active report whose `last_run_at` was
older than its cadence — so a "daily" report would fire at whatever
5-minute boundary the operator happened to create it on. Store
managers asked for a fixed wall-clock time: 21:00 daily (after close),
08:00 Monday weekly.

New columns:
  • time_of_day TIME NULL   — local wall-clock fire time. NULL keeps
                              the legacy any-time-after-cadence behaviour.
  • day_of_week INT NULL    — 0=Mon..6=Sun, only meaningful for the
                              weekly cadence. NULL = any day.
  • last_fire_date DATE NULL — bumped to today's local date on each
                              fire so we don't fire twice within the
                              dispatcher's 5-minute window-of-tolerance.
  • whatsapp_recipients TEXT NULL — comma-separated `whatsapp:+...`
                              numbers to ping alongside email.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scheduled_reports",
        sa.Column("time_of_day", sa.Time, nullable=True))
    op.add_column("scheduled_reports",
        sa.Column("day_of_week", sa.Integer, nullable=True))
    op.add_column("scheduled_reports",
        sa.Column("last_fire_date", sa.Date, nullable=True))
    op.add_column("scheduled_reports",
        sa.Column("whatsapp_recipients", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_reports", "whatsapp_recipients")
    op.drop_column("scheduled_reports", "last_fire_date")
    op.drop_column("scheduled_reports", "day_of_week")
    op.drop_column("scheduled_reports", "time_of_day")
