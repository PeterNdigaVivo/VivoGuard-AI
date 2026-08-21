"""fail-closed training provenance

Revision ID: 0038
Revises: 0037
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("training_images", sa.Column("source_kind", sa.String(32),
                  nullable=False, server_default="operator_verified"))
    op.add_column("training_images", sa.Column("eligible_for_training", sa.Boolean(),
                  nullable=False, server_default=sa.text("true")))
    op.add_column("training_images", sa.Column("review_state", sa.String(24),
                  nullable=False, server_default="approved"))
    op.add_column("training_images", sa.Column("simulation_run_id", sa.String(64), nullable=True))
    op.create_index("ix_training_images_source_kind", "training_images", ["source_kind"])
    op.create_index("ix_training_images_eligible_for_training", "training_images", ["eligible_for_training"])
    op.create_index("ix_training_images_review_state", "training_images", ["review_state"])
    op.create_index("ix_training_images_simulation_run_id", "training_images", ["simulation_run_id"])
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "UPDATE training_images SET source_kind='auto_live_uniform_miner', "
            "eligible_for_training=false, review_state='pending' "
            "WHERE source_extra->>'source'='simulation'"))


def downgrade() -> None:
    op.drop_index("ix_training_images_simulation_run_id", table_name="training_images")
    op.drop_index("ix_training_images_review_state", table_name="training_images")
    op.drop_index("ix_training_images_eligible_for_training", table_name="training_images")
    op.drop_index("ix_training_images_source_kind", table_name="training_images")
    op.drop_column("training_images", "simulation_run_id")
    op.drop_column("training_images", "review_state")
    op.drop_column("training_images", "eligible_for_training")
    op.drop_column("training_images", "source_kind")
