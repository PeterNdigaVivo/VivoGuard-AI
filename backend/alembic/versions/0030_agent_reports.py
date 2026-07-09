"""agent_reports — structured output of the autonomous monitoring agents

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-09

One row per agent run. `findings`, `actions_taken`, and `gaps` are stored
as JSONB on Postgres (portable JSON on the SQLite edge build). Pruned to a
30-day rolling window by the Inspection agent (agents.agent_inspection).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSONB on Postgres, plain JSON on SQLite (edge profile) — keeps both
# deployment shapes working, matching the rest of the models.
_JSON = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
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
    op.create_index("ix_agent_reports_name_run_at", "agent_reports",
                    ["agent_name", "run_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_reports_name_run_at", table_name="agent_reports")
    op.drop_table("agent_reports")
