"""operations assurance and governed event fusion

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("critical_zone_requirements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False), sa.Column("zone_kind", sa.String(32), nullable=False),
        sa.Column("zone_id", sa.Integer(), sa.ForeignKey("zones.id", ondelete="SET NULL")),
        sa.Column("required_camera_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_frame_age_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("requires_incident_clip", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "name", name="uq_critical_zone_store_name"))
    op.create_index("ix_critical_zone_requirements_store_id", "critical_zone_requirements", ["store_id"])
    op.create_index("ix_critical_zone_requirements_zone_kind", "critical_zone_requirements", ["zone_kind"])
    op.create_index("ix_critical_zone_requirements_is_active", "critical_zone_requirements", ["is_active"])

    op.create_table("assurance_cases",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("case_type", sa.String(40), nullable=False), sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="SET NULL")),
        sa.Column("camera_id", sa.Integer(), sa.ForeignKey("cameras.id", ondelete="SET NULL")),
        sa.Column("zone_id", sa.Integer(), sa.ForeignKey("zones.id", ondelete="SET NULL")),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="SET NULL")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("detection_events.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(255), nullable=False), sa.Column("description", sa.Text()),
        sa.Column("root_cause", sa.String(64)), sa.Column("evidence", sa.JSON()), sa.Column("label_json", sa.JSON()),
        sa.Column("training_status", sa.String(40)),
        sa.Column("human_review_required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("assigned_to", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)), sa.Column("resolution", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)))
    op.create_index("ix_assurance_cases_dedup_key", "assurance_cases", ["dedup_key"], unique=True)
    op.create_index("ix_assurance_cases_case_type", "assurance_cases", ["case_type"])
    op.create_index("ix_assurance_cases_severity", "assurance_cases", ["severity"])
    op.create_index("ix_assurance_cases_status", "assurance_cases", ["status"])
    op.create_index("ix_assurance_cases_store_id", "assurance_cases", ["store_id"])
    op.create_index("ix_assurance_cases_store_type_status", "assurance_cases", ["store_id", "case_type", "status"])

    op.create_table("operational_events",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Float()), sa.Column("currency", sa.String(8)), sa.Column("actor_ref", sa.String(128)),
        sa.Column("transaction_ref", sa.String(128)), sa.Column("payload", sa.JSON()),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source", "source_event_id", name="uq_operational_event_source_id"))
    op.create_index("ix_operational_events_source", "operational_events", ["source"])
    op.create_index("ix_operational_events_store_id", "operational_events", ["store_id"])
    op.create_index("ix_operational_events_event_type", "operational_events", ["event_type"])
    op.create_index("ix_operational_events_occurred_at", "operational_events", ["occurred_at"])
    op.create_index("ix_operational_events_store_time", "operational_events", ["store_id", "occurred_at"])

    op.create_table("risk_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operational_event_id", sa.Integer(), sa.ForeignKey("operational_events.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("risk_type", sa.String(40), nullable=False), sa.Column("score", sa.Float(), nullable=False),
        sa.Column("band", sa.String(16), nullable=False), sa.Column("factors", sa.JSON(), nullable=False),
        sa.Column("camera_evidence", sa.JSON()), sa.Column("status", sa.String(24), nullable=False, server_default="pending_human_review"),
        sa.Column("human_review_required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)), sa.Column("conclusion", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index("ix_risk_reviews_store_id", "risk_reviews", ["store_id"])
    op.create_index("ix_risk_reviews_risk_type", "risk_reviews", ["risk_type"])
    op.create_index("ix_risk_reviews_band", "risk_reviews", ["band"])
    op.create_index("ix_risk_reviews_status", "risk_reviews", ["status"])

    op.create_table("governance_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_email", sa.String(255)), sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False), sa.Column("entity_id", sa.String(128)),
        sa.Column("details", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index("ix_governance_audit_log_action", "governance_audit_log", ["action"])
    op.create_index("ix_governance_audit_log_entity_type", "governance_audit_log", ["entity_type"])
    op.create_index("ix_governance_audit_log_created_at", "governance_audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("governance_audit_log")
    op.drop_table("risk_reviews")
    op.drop_table("operational_events")
    op.drop_table("assurance_cases")
    op.drop_table("critical_zone_requirements")
