"""enforce case-insensitive normalised store identity

Revision ID: 0043
Revises: 0042
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Application checks provide friendly errors; these functional indexes
    # close the concurrent-write race and make the invariant authoritative.
    op.execute("""
        CREATE UNIQUE INDEX uq_stores_name_normalised
        ON stores ((lower(btrim(name))))
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_stores_code_normalised
        ON stores ((lower(btrim(code))))
        WHERE code IS NOT NULL AND btrim(code) <> ''
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_stores_code_normalised")
    op.execute("DROP INDEX IF EXISTS uq_stores_name_normalised")
