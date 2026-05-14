"""Lumana parity additions: scheduled_reports + customer_journeys

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- scheduled_reports (P2) ----
    op.create_table(
        "scheduled_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True),
        # "daily" | "weekly"
        sa.Column("cadence", sa.String(16), nullable=False, server_default="daily"),
        # "csv" | "pdf"
        sa.Column("format", sa.String(8), nullable=False, server_default="pdf"),
        # comma-separated email recipients
        sa.Column("recipients", sa.Text, nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- customer_journeys (P3) ----
    op.create_table(
        "customer_journeys",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("day", sa.Date, nullable=False, index=True),
        sa.Column("track_signature", sa.String(128), nullable=False),
        # ordered list of {"zone_id":..., "zone_name":..., "entered_at":..., "exited_at":...}
        sa.Column("zone_sequence_json", sa.JSON, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at",   sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_journeys_store_day", "customer_journeys",
                    ["store_id", "day"])

    # ---- detection_events: optional plate column for fast LPR lookup ----
    op.add_column("detection_events",
                  sa.Column("plate", sa.String(16), nullable=True))
    op.create_index("ix_detection_events_plate", "detection_events", ["plate"])


def downgrade() -> None:
    op.drop_index("ix_detection_events_plate", table_name="detection_events")
    op.drop_column("detection_events", "plate")
    op.drop_index("ix_journeys_store_day", table_name="customer_journeys")
    op.drop_table("customer_journeys")
    op.drop_table("scheduled_reports")
