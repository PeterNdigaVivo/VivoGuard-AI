"""Model-evaluation test set: eval_samples

Revision ID: 0048
Revises: 0047

One row per evaluation test image/clip (files live under EVAL_DIR on the
server, never in git): reviewed alerts with their clean frame and the
operator's verdict, busy-hour frames labelled by admins clicking each
person, and short clips labelled with a people count.
Idempotent guard per house style.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    if "eval_samples" in _inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "eval_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("alert_id", sa.Integer(),
                  sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("camera_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("detection_type", sa.String(64), nullable=True),
        sa.Column("verdict", sa.String(8), nullable=True),
        sa.Column("frame_path", sa.String(512), nullable=True),
        sa.Column("clip_path", sa.String(512), nullable=True),
        sa.Column("zone_polygon", sa.JSON(), nullable=True),
        sa.Column("event_bbox", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("label", sa.JSON(), nullable=True),
        sa.Column("labelled_by", sa.Integer(), nullable=True),
        sa.Column("labelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    for col in ("kind", "alert_id", "camera_id", "category", "status", "created_at"):
        op.create_index(f"ix_eval_samples_{col}", "eval_samples", [col])


def downgrade() -> None:
    op.drop_table("eval_samples")
