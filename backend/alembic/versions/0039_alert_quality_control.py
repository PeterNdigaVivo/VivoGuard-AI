"""alert quality quarantine and evidence eligibility

Revision ID: 0039
Revises: 0038
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, default in (("review_only", "false"),
                          ("training_eligible", "true"),
                          ("notification_suppressed", "false")):
        op.add_column("alerts", sa.Column(name, sa.Boolean(), nullable=False,
                                           server_default=sa.text(default)))
        op.create_index(f"ix_alerts_{name}", "alerts", [name])
    op.create_table(
        "alert_quality_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_id", sa.Integer(), sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detection_type", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False, server_default="active"),
        sa.Column("source", sa.String(16), nullable=False, server_default="automatic"),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_count_at_quarantine", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_false_rate", sa.Float(), nullable=True),
        sa.UniqueConstraint("camera_id", "detection_type", name="uq_alert_quality_pair"),
    )
    for name in ("camera_id", "detection_type", "mode"):
        op.create_index(f"ix_alert_quality_controls_{name}",
                        "alert_quality_controls", [name])


def downgrade() -> None:
    op.drop_table("alert_quality_controls")
    for name in ("notification_suppressed", "training_eligible", "review_only"):
        op.drop_index(f"ix_alerts_{name}", table_name="alerts")
        op.drop_column("alerts", name)
