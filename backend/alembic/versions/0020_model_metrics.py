"""model_metrics table — per (model, detection_type, day) precision + FPR

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-12

Backs the self-improving training pipeline's drift dashboard:
operator-marked confirmed/dismissed alerts are aggregated nightly
into one row per (model_id, detection_type, day). Precision and
FP-rate fall straight out of that count; recall needs ground truth
beyond what the operator sees, so the column is reserved for future
shadow-eval comparisons (Phase 3).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_metrics",
        sa.Column("id",             sa.Integer, primary_key=True),
        sa.Column("model_id",       sa.Integer,
                  sa.ForeignKey("ai_models.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("detection_type", sa.String(32), nullable=False, index=True),
        sa.Column("day",            sa.Date,       nullable=False, index=True),
        sa.Column("tp_count",       sa.Integer,    nullable=False, server_default="0"),
        sa.Column("fp_count",       sa.Integer,    nullable=False, server_default="0"),
        sa.Column("alerts_total",   sa.Integer,    nullable=False, server_default="0"),
        sa.Column("precision",      sa.Float,      nullable=True),
        sa.Column("fp_rate",        sa.Float,      nullable=True),
        # Reserved for Phase 3 shadow-eval recall computation.
        sa.Column("recall",         sa.Float,      nullable=True),
        sa.Column("computed_at",    sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_model_metrics__model_type_day",
        "model_metrics",
        ["model_id", "detection_type", "day"],
    )
    op.create_index(
        "ix_model_metrics__type_day",
        "model_metrics",
        ["detection_type", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_metrics__type_day", table_name="model_metrics")
    op.drop_constraint("uq_model_metrics__model_type_day", "model_metrics",
                       type_="unique")
    op.drop_table("model_metrics")
