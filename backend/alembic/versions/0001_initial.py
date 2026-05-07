"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- users ----
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- nvr_devices ----
    op.create_table(
        "nvr_devices",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("brand", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("sdk_port", sa.Integer, nullable=True),
        sa.Column("rtsp_port", sa.Integer, server_default="554", nullable=False),
        sa.Column("http_port", sa.Integer, server_default="80", nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password_encrypted", sa.Text, nullable=False),
        sa.Column("total_channels", sa.Integer, server_default="0", nullable=False),
        sa.Column("connected_channels", sa.Integer, server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- ai_models (created before cameras + training_jobs FKs) ----
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("base_model", sa.String(32), nullable=False),
        sa.Column("classes_json", sa.JSON, nullable=False),
        sa.Column("map50", sa.Float, nullable=True),
        sa.Column("map50_95", sa.Float, nullable=True),
        sa.Column("precision", sa.Float, nullable=True),
        sa.Column("recall", sa.Float, nullable=True),
        sa.Column("weights_path", sa.Text, nullable=False),
        sa.Column("export_format", sa.String(16), nullable=True),
        sa.Column("deployed", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("is_base", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("training_job_id", sa.Integer, nullable=True),  # FK added after training_jobs
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- cameras ----
    op.create_table(
        "cameras",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("site", sa.String(128), nullable=True),
        sa.Column("brand", sa.String(32), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("public_ip", sa.String(64), nullable=True),
        sa.Column("sdk_port", sa.Integer, nullable=True),
        sa.Column("rtsp_port", sa.Integer, server_default="554", nullable=False),
        sa.Column("http_port", sa.Integer, server_default="80", nullable=False),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("password_encrypted", sa.Text, nullable=True),
        sa.Column("nvr_id", sa.Integer, sa.ForeignKey("nvr_devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel_number", sa.Integer, nullable=True),
        sa.Column("rtsp_url_override", sa.Text, nullable=True),
        sa.Column("ddns_hostname", sa.String(255), nullable=True),
        sa.Column("network_type", sa.String(8), server_default="lan", nullable=False),
        sa.Column("ai_model_id", sa.Integer, sa.ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ai_enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("inference_fps", sa.Integer, server_default="5", nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- detection_configs ----
    op.create_table(
        "detection_configs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("detection_type", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("confidence_threshold", sa.Float, server_default="0.6", nullable=False),
        sa.Column("min_object_size", sa.Integer, server_default="0", nullable=False),
        sa.Column("detection_every_n_frames", sa.Integer, server_default="1", nullable=False),
        sa.Column("dwell_time_seconds", sa.Integer, nullable=True),
        sa.Column("crowd_threshold", sa.Integer, nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("schedule_json", sa.JSON, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("camera_id", "detection_type", name="uq_detection_camera_type"),
    )

    # ---- zones ----
    op.create_table(
        "zones",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("shape", sa.String(16), server_default="polygon", nullable=False),
        sa.Column("polygon_coords_json", sa.JSON, nullable=False),
        sa.Column("detection_types_json", sa.JSON, nullable=False),
        sa.Column("active_schedule_json", sa.JSON, nullable=True),
        sa.Column("suppressed", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ---- detection_events ----
    op.create_table(
        "detection_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL"), nullable=True),
        sa.Column("detection_type", sa.String(32), nullable=False, index=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("bbox_json", sa.JSON, nullable=False),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
        sa.Column("thumbnail_path", sa.Text, nullable=True),
        sa.Column("clip_path", sa.Text, nullable=True),
        sa.Column("model_id", sa.Integer, sa.ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed", sa.Boolean, server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        "ix_detection_events_camera_ts",
        "detection_events",
        ["camera_id", sa.text("timestamp DESC")],
    )

    # ---- alerts ----
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_id", sa.Integer, sa.ForeignKey("detection_events.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("status", sa.String(16), server_default="new", nullable=False, index=True),
        sa.Column("assigned_to", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_used_for_training", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    # ---- training: datasets, images, annotations, jobs ----
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("classes_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "training_images",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dataset_id", sa.Integer, sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("labeled", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("split", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("image_id", sa.Integer, sa.ForeignKey("training_images.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("class_label", sa.String(64), nullable=False),
        sa.Column("bbox_json", sa.JSON, nullable=False),
        sa.Column("annotated_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("verified", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("auto_suggested", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "training_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("dataset_id", sa.Integer, sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("config_json", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("current_epoch", sa.Integer, server_default="0", nullable=False),
        sa.Column("total_epochs", sa.Integer, server_default="0", nullable=False),
        sa.Column("best_map50", sa.Float, nullable=True),
        sa.Column("log_path", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Now that training_jobs exists, add the deferred FK on ai_models.
    op.create_foreign_key(
        "fk_ai_models_training_job",
        "ai_models", "training_jobs",
        ["training_job_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ai_models_training_job", "ai_models", type_="foreignkey")
    op.drop_table("training_jobs")
    op.drop_table("annotations")
    op.drop_table("training_images")
    op.drop_table("datasets")
    op.drop_table("alerts")
    op.drop_index("ix_detection_events_camera_ts", table_name="detection_events")
    op.drop_table("detection_events")
    op.drop_table("zones")
    op.drop_table("detection_configs")
    op.drop_table("cameras")
    op.drop_table("ai_models")
    op.drop_table("nvr_devices")
    op.drop_table("users")
