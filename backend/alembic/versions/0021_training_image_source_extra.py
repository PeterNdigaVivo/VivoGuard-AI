"""training_images.source_extra — evidence payload from the originating alert

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-14

When an alert is reviewed (✅ Correct / ❌ False), the feedback loop
now stamps the resulting TrainingImage with the full evidence
payload it can recover from the originating DetectionEvent: alert
type, store, camera, timestamp, zone, confidence, detection bbox,
extra dims (person count, tracking ids, etc). Keeps the labelled
sample fully self-describing so dataset curators / future trainers
can filter/dedup without re-joining back to the alert row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "training_images",
        sa.Column("source_extra", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("training_images", "source_extra")
