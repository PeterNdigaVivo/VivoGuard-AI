"""staff_tracks.source — uniform vs zone identification

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26

Adds a `source` column to staff_tracks so the dashboard can show how
each staff member was identified: 'zone' (>10 min in a counter zone,
the dwell classifier) or 'uniform' (the uniform-compliance detector
saw a compliant Vivo uniform). Existing rows default to 'zone'.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "staff_tracks",
        sa.Column("source", sa.String(16), nullable=False, server_default="zone"),
    )


def downgrade() -> None:
    op.drop_column("staff_tracks", "source")
