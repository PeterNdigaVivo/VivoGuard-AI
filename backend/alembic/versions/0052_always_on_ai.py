"""cameras.always_on_ai — permanent inference slot (no rotation)

Revision ID: 0052
Revises: 0051

Pinned cameras get long inference slices at elevated queue priority so
they hold a worker slot continuously instead of rotating with the
fleet. Guarded so a re-run is harmless.
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "always_on_ai" not in _cols(bind, "cameras"):
        op.add_column("cameras", sa.Column(
            "always_on_ai", sa.Boolean(), nullable=False,
            server_default=sa.text("false")))


def downgrade() -> None:
    bind = op.get_bind()
    if "always_on_ai" in _cols(bind, "cameras"):
        op.drop_column("cameras", "always_on_ai")
