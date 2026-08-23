"""read-only Odoo store, roster, till and conversion projections

Revision ID: 0041
Revises: 0040
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "odoo_store_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("odoo_model", sa.String(64), nullable=False, server_default="pos.config"),
        sa.Column("odoo_res_id", sa.Integer(), nullable=False),
        sa.Column("odoo_pos_config_id", sa.Integer(), nullable=True),
        sa.Column("code", sa.String(32), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Nairobi"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_error", sa.String(500), nullable=True),
        sa.UniqueConstraint("store_id", name="uq_odoo_store_map_store_id"),
        sa.UniqueConstraint("odoo_model", "odoo_res_id", name="uq_odoo_store_model_res"),
    )
    for name in ("store_id", "odoo_res_id", "odoo_pos_config_id", "code"):
        op.create_index(f"ix_odoo_store_map_{name}", "odoo_store_map", [name])

    op.create_table(
        "store_business_hours",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=True),
        sa.Column("close_time", sa.Time(), nullable=True),
        sa.Column("source", sa.String(24), nullable=False, server_default="manual"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "day_of_week", "source", name="uq_store_hours_day_source"),
    )
    op.create_index("ix_store_business_hours_store_id", "store_business_hours", ["store_id"])

    op.create_table(
        "odoo_roster_windows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("work_day", sa.Date(), nullable=False),
        sa.Column("employee_ref", sa.String(64), nullable=False),
        sa.Column("shift_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "work_day", "employee_ref", name="uq_odoo_roster_window"),
    )
    op.create_index("ix_odoo_roster_windows_store_id", "odoo_roster_windows", ["store_id"])
    op.create_index("ix_odoo_roster_windows_work_day", "odoo_roster_windows", ["work_day"])
    op.create_index("ix_odoo_roster_store_start_end", "odoo_roster_windows", ["store_id", "shift_start", "shift_end"])

    op.create_table(
        "odoo_pos_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("odoo_session_id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("odoo_config_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("odoo_session_id", name="uq_odoo_pos_sessions_odoo_session_id"),
    )
    for name in ("odoo_session_id", "store_id", "odoo_config_id", "state"):
        op.create_index(f"ix_odoo_pos_sessions_{name}", "odoo_pos_sessions", [name])

    op.create_table(
        "odoo_till_conflicts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_day", sa.Date(), nullable=False),
        sa.Column("conflict_type", sa.String(48), nullable=False),
        sa.Column("camera_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("till_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "business_day", "conflict_type", name="uq_odoo_till_conflict"),
    )
    for name in ("store_id", "business_day", "conflict_type", "status"):
        op.create_index(f"ix_odoo_till_conflicts_{name}", "odoo_till_conflicts", [name])

    op.create_table(
        "odoo_store_sales_hourly",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_total", sa.Float(), nullable=False, server_default="0"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "period_start", name="uq_odoo_sales_store_hour"),
    )
    op.create_index("ix_odoo_store_sales_hourly_store_id", "odoo_store_sales_hourly", ["store_id"])
    op.create_index("ix_odoo_store_sales_hourly_period_start", "odoo_store_sales_hourly", ["period_start"])

    op.create_table(
        "odoo_pos_activity_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "period_start", name="uq_odoo_pos_activity_bucket"),
    )
    op.create_index("ix_odoo_pos_activity_buckets_store_id", "odoo_pos_activity_buckets", ["store_id"])
    op.create_index("ix_odoo_pos_activity_buckets_period_start", "odoo_pos_activity_buckets", ["period_start"])

    op.create_table(
        "odoo_conversion_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("footfall", sa.Integer(), nullable=False),
        sa.Column("transactions", sa.Integer(), nullable=False),
        sa.Column("conversion_rate", sa.Float(), nullable=True),
        sa.Column("data_quality_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "period_start", name="uq_odoo_conversion_store_hour"),
    )
    op.create_index("ix_odoo_conversion_metrics_store_id", "odoo_conversion_metrics", ["store_id"])
    op.create_index("ix_odoo_conversion_metrics_period_start", "odoo_conversion_metrics", ["period_start"])

    op.create_table(
        "odoo_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream", sa.String(48), nullable=False),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.UniqueConstraint("stream", name="uq_odoo_sync_state_stream"),
    )
    op.create_index("ix_odoo_sync_state_stream", "odoo_sync_state", ["stream"])


def downgrade() -> None:
    for table in (
        "odoo_sync_state", "odoo_conversion_metrics", "odoo_pos_activity_buckets",
        "odoo_store_sales_hourly",
        "odoo_till_conflicts", "odoo_pos_sessions", "odoo_roster_windows",
        "store_business_hours", "odoo_store_map",
    ):
        op.drop_table(table)
