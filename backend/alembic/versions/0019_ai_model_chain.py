"""ai_models: chain-model + detector_type columns

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-03

Adds columns the chain-training overhaul needs: is_chain_model,
detector_type ('uniform' / 'shutter'), trained_on_stores (jsonb of
store ids that contributed), sample_count (dataset size). All
nullable / defaulted so existing rows backfill cleanly.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_models",
        sa.Column("is_chain_model", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("ai_models",
        sa.Column("detector_type", sa.String(32), nullable=True))
    op.add_column("ai_models",
        sa.Column("trained_on_stores", postgresql.JSONB, nullable=True))
    op.add_column("ai_models",
        sa.Column("sample_count", sa.Integer, nullable=True))
    op.create_index("ix_ai_models__detector_chain",
                    "ai_models", ["detector_type", "is_chain_model"])


def downgrade() -> None:
    op.drop_index("ix_ai_models__detector_chain", table_name="ai_models")
    op.drop_column("ai_models", "sample_count")
    op.drop_column("ai_models", "trained_on_stores")
    op.drop_column("ai_models", "detector_type")
    op.drop_column("ai_models", "is_chain_model")
