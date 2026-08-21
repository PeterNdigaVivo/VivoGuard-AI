"""camera soft-delete lifecycle audit

Revision ID: 0036
Revises: 0035
"""
from alembic import op
import sqlalchemy as sa


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column(
        "is_deleted", sa.Boolean(), nullable=False,
        server_default=sa.text("false")))
    op.add_column("cameras", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cameras", sa.Column("deleted_by_user_id", sa.Integer(), nullable=True))
    op.add_column("cameras", sa.Column("deleted_by_email", sa.String(length=255), nullable=True))
    op.add_column("cameras", sa.Column("deleted_previous_status", sa.String(length=16), nullable=True))
    op.add_column("cameras", sa.Column("deleted_previous_ai_enabled", sa.Boolean(), nullable=True))
    op.add_column("cameras", sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cameras", sa.Column("restored_by_user_id", sa.Integer(), nullable=True))
    op.add_column("cameras", sa.Column("restored_by_email", sa.String(length=255), nullable=True))
    op.create_index("ix_cameras_is_deleted", "cameras", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_cameras_is_deleted", table_name="cameras")
    op.drop_column("cameras", "restored_by_email")
    op.drop_column("cameras", "restored_by_user_id")
    op.drop_column("cameras", "restored_at")
    op.drop_column("cameras", "deleted_previous_ai_enabled")
    op.drop_column("cameras", "deleted_previous_status")
    op.drop_column("cameras", "deleted_by_email")
    op.drop_column("cameras", "deleted_by_user_id")
    op.drop_column("cameras", "deleted_at")
    op.drop_column("cameras", "is_deleted")
