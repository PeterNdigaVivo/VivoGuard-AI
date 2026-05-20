"""staff_tracks classifier table

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-20

A daily-rolled-up classification per (store, day, track) flagging
whether the track belongs to a staff member or a customer.

The classifier (app/tasks/staff_classifier.py) runs every 10 minutes
and inspects customer_journeys for the day. Any track that
accumulated >10 minutes of dwell time inside a counter zone is
marked 'staff'; everyone else stays 'customer'. The analytics
endpoints LEFT-JOIN visitor_tracks against this table and exclude
'staff'-flagged signatures from visitor counts, dwell averages,
journey aggregates, etc.

Future upgrade path (commented in the classifier source): once we
ship a uniform-detection model or face-recognition for staff
enrollment, we'll positively flag staff at write-time rather than
inferring after-the-fact.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_tracks",
        sa.Column("id",              sa.Integer, primary_key=True),
        sa.Column("store_id",        sa.Integer,
                  sa.ForeignKey("stores.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("day",             sa.Date,    nullable=False, index=True),
        sa.Column("track_signature", sa.String(128), nullable=False),
        sa.Column("first_seen",      sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen",       sa.DateTime(timezone=True), nullable=False),
        # Seconds the track spent inside any counter zone. >600s = staff.
        sa.Column("counter_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("classified_as",   sa.String(16), nullable=False,
                  server_default="customer"),     # staff | customer
        sa.Column("created_at",      sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "day", "track_signature",
                            name="uq_staff_tracks_store_day_sig"),
    )


def downgrade() -> None:
    op.drop_table("staff_tracks")
