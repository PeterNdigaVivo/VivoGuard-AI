"""training_images.source_alert_id FK on alerts

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-23

Adds nullable FK from training_images.source_alert_id → alerts.id with
ondelete=SET NULL. The feedback-loop (absorb_confirmed / mark_dismissed)
stamps this on every row it writes so the sprint UI's "undo" can
delete exactly the training image that came from a given alert,
without fragile (camera_id, file_path) matching.

ondelete=SET NULL chosen over CASCADE intentionally: if an alert row
is ever pruned (retention sweep), the training image must survive —
it has independent value. The NULL just means "we can no longer
trace this sample back to its originating alert."
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "training_images",
        sa.Column("source_alert_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_training_images__source_alert",
        "training_images", "alerts",
        ["source_alert_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_training_images__source_alert_id",
        "training_images",
        ["source_alert_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_training_images__source_alert_id",
                  table_name="training_images")
    op.drop_constraint("fk_training_images__source_alert",
                       "training_images", type_="foreignkey")
    op.drop_column("training_images", "source_alert_id")
