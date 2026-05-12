"""retail foundation: stores, shifts, metric_snapshots, visitor_tracks, stockroom_accesses, campaigns

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

Purely additive. Existing cameras keep working; the new `cameras.store_id`
column is nullable and defaults to NULL.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- stores ----
    op.create_table(
        "stores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("code", sa.String(32), nullable=True, unique=True),
        sa.Column("country", sa.String(64), nullable=False),
        sa.Column("city", sa.String(128), nullable=True),
        sa.Column("address", sa.Text, nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Nairobi"),
        sa.Column("business_hours_json", sa.JSON, nullable=True),
        sa.Column("capacity", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- shifts ----
    op.create_table(
        "shifts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("day_of_week", sa.Integer, nullable=False),
        sa.Column("start_time", sa.Time, nullable=False),
        sa.Column("end_time", sa.Time, nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
    )

    # ---- metric_snapshots ----
    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL"), nullable=True),
        sa.Column("metric_type", sa.String(48), nullable=False, index=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("dims", sa.JSON, nullable=True),
    )
    op.create_index(
        "ix_metrics_store_type_period", "metric_snapshots",
        ["store_id", "metric_type", sa.text("period_start DESC")],
    )

    # ---- visitor_tracks ----
    op.create_table(
        "visitor_tracks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("day", sa.Date, nullable=False, index=True),
        sa.Column("track_signature", sa.String(128), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen",  sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("demographic_age", sa.String(16), nullable=True),
        sa.Column("demographic_gender", sa.String(8), nullable=True),
    )
    op.create_index(
        "ix_visitor_track_dedup_key", "visitor_tracks",
        ["store_id", "day", "track_signature"], unique=True,
    )

    # ---- stockroom_accesses ----
    op.create_table(
        "stockroom_accesses",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL"), nullable=True),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("track_id", sa.Integer, nullable=False),
        sa.Column("snapshot_path", sa.String(512), nullable=True),
        sa.Column("is_authorised", sa.Boolean, nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_index(
        "ix_stockroom_store_ts", "stockroom_accesses",
        ["store_id", sa.text("timestamp DESC")],
    )

    # ---- campaigns ----
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- cameras.store_id (nullable; existing rows stay NULL) ----
    op.add_column(
        "cameras",
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_cameras_store_id", "cameras", ["store_id"])


def downgrade() -> None:
    op.drop_index("ix_cameras_store_id", table_name="cameras")
    op.drop_column("cameras", "store_id")
    op.drop_table("campaigns")
    op.drop_index("ix_stockroom_store_ts", table_name="stockroom_accesses")
    op.drop_table("stockroom_accesses")
    op.drop_index("ix_visitor_track_dedup_key", table_name="visitor_tracks")
    op.drop_table("visitor_tracks")
    op.drop_index("ix_metrics_store_type_period", table_name="metric_snapshots")
    op.drop_table("metric_snapshots")
    op.drop_table("shifts")
    op.drop_table("stores")
