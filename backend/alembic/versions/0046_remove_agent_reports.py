"""Remove the autonomous monitoring agents' report storage.

Revision ID: 0046
Revises: 0045
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.drop_index("ix_agent_reports_name_run_at", table_name="agent_reports")
    op.drop_table("agent_reports")


def downgrade() -> None:
    op.create_table(
        "agent_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("findings", _JSON, nullable=True),
        sa.Column("actions_taken", _JSON, nullable=True),
        sa.Column("gaps", _JSON, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_agent_reports_name_run_at", "agent_reports", ["agent_name", "run_at"]
    )
