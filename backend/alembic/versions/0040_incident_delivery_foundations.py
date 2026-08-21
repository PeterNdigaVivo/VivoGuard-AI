"""incident lifecycle, canonical evidence and delivery outbox foundations

Revision ID: 0040
Revises: 0039
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_key", sa.String(200), nullable=False),
        sa.Column("camera_id", sa.Integer(), sa.ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("detection_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("current_state", sa.String(24), nullable=False, server_default="provisional"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_key", name="uq_incidents_incident_key"),
    )
    for name in ("incident_key", "camera_id", "store_id", "detection_type", "current_state", "opened_at"):
        op.create_index(f"ix_incidents_{name}", "incidents", [name])

    op.create_table(
        "incident_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("detection_events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_event_uuid", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("alert_id", name="uq_incident_members_alert_id"),
        sa.UniqueConstraint("event_id", name="uq_incident_members_event_id"),
        sa.UniqueConstraint("source_event_uuid", name="uq_incident_members_source_uuid"),
        sa.UniqueConstraint("idempotency_key", name="uq_incident_members_idempotency"),
    )
    for name in ("incident_id", "alert_id", "event_id", "idempotency_key"):
        op.create_index(f"ix_incident_members_{name}", "incident_members", [name])

    op.create_table(
        "incident_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for name in ("incident_id", "to_state", "created_at"):
        op.create_index(f"ix_incident_transitions_{name}", "incident_transitions", [name])

    op.create_table(
        "evidence_manifests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        sa.Column("clip_path", sa.Text(), nullable=True),
        sa.Column("filmstrip_paths_json", sa.JSON(), nullable=True),
        sa.Column("clip_eligible", sa.Boolean(), nullable=True),
        sa.Column("clip_available", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ineligible_reason", sa.String(128), nullable=True),
        sa.Column("snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("clip_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("alert_id", name="uq_evidence_manifests_alert_id"),
    )
    op.create_index("ix_evidence_manifests_alert_id", "evidence_manifests", ["alert_id"])
    op.create_index("ix_evidence_manifests_clip_eligible", "evidence_manifests", ["clip_eligible"])
    op.create_index("ix_evidence_manifests_clip_available", "evidence_manifests", ["clip_available"])

    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("destination_ref", sa.String(255), nullable=False),
        sa.Column("payload_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_delivery_outbox_idempotency"),
    )
    for name in ("idempotency_key", "incident_id", "alert_id", "channel", "status", "available_at"):
        op.create_index(f"ix_delivery_outbox_{name}", "delivery_outbox", [name])


def downgrade() -> None:
    op.drop_table("delivery_outbox")
    op.drop_table("evidence_manifests")
    op.drop_table("incident_transitions")
    op.drop_table("incident_members")
    op.drop_table("incidents")
