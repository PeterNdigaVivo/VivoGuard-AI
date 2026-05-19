"""rtsp_transport flag + uniview brand

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-19

Two changes:

  1. cameras.rtsp_transport — selects FFmpeg's -rtsp_transport flag.
     Default 'tcp' (the existing implicit behaviour). 'http' enables
     RTSP-over-HTTP tunneling for NVRs behind routers that block 554
     but pass HTTP ports (80, 8000, 8080, 7000, 800 — the Vivo fleet
     pattern in Kenya/Uganda/Rwanda). 'udp' for the rare case where
     a network performs better on UDP than TCP.

  2. No DDL needed for the brand column itself — it's an unconstrained
     string. We just widen the Pydantic validator in the API schema
     to accept 'uniview' alongside dahua/hikvision/onvif/generic.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras",
        sa.Column("rtsp_transport", sa.String(8),
                  nullable=False, server_default="tcp"))


def downgrade() -> None:
    op.drop_column("cameras", "rtsp_transport")
