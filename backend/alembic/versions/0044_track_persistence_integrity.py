"""make track persistence concurrency-safe

Revision ID: 0044
Revises: 0043
"""
import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Production source tags such as ``afterhours_present`` and
    # ``uniform_dual_black`` legitimately exceed the original 16 chars.
    op.alter_column(
        "staff_tracks",
        "source",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default=sa.text("'zone'::character varying"),
    )


def downgrade() -> None:
    # Refuse a lossy downgrade if newer source tags are present.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM staff_tracks WHERE length(source) > 16) THEN
                RAISE EXCEPTION
                    'cannot downgrade: staff_tracks.source contains values longer than 16';
            END IF;
        END $$
    """)
    op.alter_column(
        "staff_tracks",
        "source",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'zone'::character varying"),
    )
