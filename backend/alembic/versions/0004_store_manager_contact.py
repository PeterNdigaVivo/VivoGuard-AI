"""store_manager_contact: add manager name + phone for WhatsApp routing

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16

Purely additive — both columns nullable so existing stores stay valid.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("manager_name",  sa.String(128), nullable=True))
    op.add_column("stores", sa.Column("manager_phone", sa.String(32),  nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "manager_phone")
    op.drop_column("stores", "manager_name")
