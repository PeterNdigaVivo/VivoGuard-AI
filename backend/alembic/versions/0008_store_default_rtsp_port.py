"""store-level default RTSP port

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-19

Most Vivo stores share an NVR vendor pattern (Dahua-on-7000 is by
far the most common across the 26 stores in Kenya/Uganda/Rwanda).
Letting an operator set the store's default RTSP port once means
each camera added to that store inherits the right port without
re-typing — and gives us a one-shot fixup target ("set all Moi Ave
cameras to 7000") via the bulk-update-port endpoint.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores",
        sa.Column("default_rtsp_port", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "default_rtsp_port")
