"""queue SLA columns on stores

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-30

Per-store queue SLA targets for the Queue Intelligence dashboard.
queue_sla_seconds (default 180 = 3 min) is the wait-time target;
queue_sla_length (default 6) is the queue-length target.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores",
        sa.Column("queue_sla_seconds", sa.Integer, nullable=False, server_default="180"))
    op.add_column("stores",
        sa.Column("queue_sla_length", sa.Integer, nullable=False, server_default="6"))


def downgrade() -> None:
    op.drop_column("stores", "queue_sla_length")
    op.drop_column("stores", "queue_sla_seconds")
