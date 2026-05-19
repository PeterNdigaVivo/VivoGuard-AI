"""camera transport: rtsp | http_snapshot

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-19

Lets operators switch a camera from RTSP to HTTP-snapshot polling
when the store router doesn't forward port 554 but DOES forward the
NVR's HTTP port (common Dahua case: HTTP=7000, RTSP=554 blocked).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras",
        sa.Column("transport", sa.String(16),
                  nullable=False, server_default="rtsp"))
    op.add_column("cameras",
        sa.Column("snapshot_url_override", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "snapshot_url_override")
    op.drop_column("cameras", "transport")
