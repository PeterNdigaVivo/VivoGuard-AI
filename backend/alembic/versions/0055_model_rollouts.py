"""Model rollouts (canary, manual promotion, rollback) and evaluations

Revision ID: 0055
Revises: 0054

Training used to end with automatic registry promotion, and some paths
rewrote Camera.ai_model_id for a whole store or every camera of a
detector without a canary. This adds the two tables behind the explicit
canary -> evaluate -> manual promote -> rollback flow
(app.services.model_rollout). Purely additive; no existing row changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind):
    from sqlalchemy import inspect
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "model_rollouts" not in tables:
        op.create_table(
            "model_rollouts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False),
            sa.Column("stage", sa.String(16), nullable=False, server_default="canary"),
            sa.Column("camera_ids_json", sa.JSON(), nullable=False),
            sa.Column("previous_assignment_json", sa.JSON(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("started_by", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("promoted_by", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rolled_back_by", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_model_rollouts_model_id", "model_rollouts", ["model_id"])
        op.create_index("ix_model_rollouts_stage", "model_rollouts", ["stage"])
    if "model_evaluations" not in tables:
        op.create_table(
            "model_evaluations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False),
            sa.Column("rollout_id", sa.Integer(),
                      sa.ForeignKey("model_rollouts.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("split", sa.String(16), nullable=False),
            sa.Column("precision", sa.Float(), nullable=True),
            sa.Column("recall", sa.Float(), nullable=True),
            sa.Column("false_positive_rate", sa.Float(), nullable=True),
            sa.Column("sample_count", sa.Integer(), nullable=True),
            sa.Column("metrics_json", sa.JSON(), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=True),
            sa.Column("reasons_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        for col in ("model_id", "rollout_id", "split", "created_at"):
            op.create_index(f"ix_model_evaluations_{col}", "model_evaluations", [col])


def downgrade() -> None:
    tables = _tables(op.get_bind())
    if "model_evaluations" in tables:
        op.drop_table("model_evaluations")
    if "model_rollouts" in tables:
        op.drop_table("model_rollouts")
