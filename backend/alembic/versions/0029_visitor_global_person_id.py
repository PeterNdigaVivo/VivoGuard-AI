"""visitor_tracks.global_person_id — cross-camera Re-ID identity

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-29

Adds the nullable cross-camera identity column written by the Sprint 2.2
Re-ID pipeline. One physical person seen on several cameras in a store
shares ONE global_person_id, so unique-visitor counting and customer-
journey queries can de-duplicate across cameras instead of counting one
shopper once per camera. NULL for every pre-Re-ID row and whenever
REID_ENABLED is false — counting then falls back to track_signature, so
this migration is backward-compatible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "visitor_tracks",
        sa.Column("global_person_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_visitor_track_global_person",
        "visitor_tracks",
        ["store_id", "day", "global_person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_visitor_track_global_person", table_name="visitor_tracks")
    op.drop_column("visitor_tracks", "global_person_id")
