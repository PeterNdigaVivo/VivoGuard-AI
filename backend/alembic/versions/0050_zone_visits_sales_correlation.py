"""zone_visits + sales_ai_correlation (Odoo sales attribution)

Revision ID: 0050
Revises: 0049

zone_visits  — one row per occupancy episode of a counter-tagged zone
               (written by the HeatmapDetector presence logger).
sales_ai_correlation — audit trail of the Odoo sale-confirmed webhook:
               every received sale and whether camera evidence of a
               customer at the counter overlapped it.

Guarded so a re-run (or a hand-created table) is harmless.
"""
from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    from sqlalchemy import inspect as _inspect
    return set(_inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    if "zone_visits" not in existing:
        op.create_table(
            "zone_visits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("store_id", sa.Integer(),
                      sa.ForeignKey("stores.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("camera_id", sa.Integer(),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("zone_id", sa.Integer(),
                      sa.ForeignKey("zones.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("entered_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("exited_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
        )
        op.create_index("ix_zone_visits_store_id", "zone_visits",
                        ["store_id"])
        op.create_index("ix_zone_visits_camera_id", "zone_visits",
                        ["camera_id"])
        op.create_index("ix_zone_visits_zone_id", "zone_visits",
                        ["zone_id"])
        op.create_index("ix_zone_visits_entered_at", "zone_visits",
                        ["entered_at"])
        # The webhook's match query filters store + time window.
        op.create_index("ix_zone_visits__store_entered", "zone_visits",
                        ["store_id", "entered_at"])
    if "sales_ai_correlation" not in existing:
        op.create_table(
            "sales_ai_correlation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("store_id", sa.Integer(),
                      sa.ForeignKey("stores.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("order_timestamp", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("amount_total", sa.Float(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("zone_visit_id", sa.Integer(),
                      sa.ForeignKey("zone_visits.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("reason", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_sales_ai_correlation_store_id",
                        "sales_ai_correlation", ["store_id"])
        op.create_index("ix_sales_ai_correlation_order_id",
                        "sales_ai_correlation", ["order_id"])
        op.create_index("ix_sales_ai_correlation_status",
                        "sales_ai_correlation", ["status"])
        op.create_index("ix_sales_ai_correlation_created_at",
                        "sales_ai_correlation", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    if "sales_ai_correlation" in existing:
        op.drop_table("sales_ai_correlation")
    if "zone_visits" in existing:
        op.drop_table("zone_visits")
