"""backfill store business hours + timezone

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-25

Stores with a NULL / empty business_hours_json were being treated as
"always after hours" by the person-alert logic, producing false
"Person Detected After Hours" alerts at 9:57 AM. Backfill the
dominant Vivo schedule (09:00-21:00 daily) and default the timezone
to Africa/Nairobi so the after-hours check has something sane to
compare against.
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_HOURS = (
    '{"mon":["09:00-21:00"],"tue":["09:00-21:00"],"wed":["09:00-21:00"],'
    '"thu":["09:00-21:00"],"fri":["09:00-21:00"],"sat":["09:00-21:00"],'
    '"sun":["09:00-21:00"]}'
)


def upgrade() -> None:
    # business_hours_json is a JSON column — compare/cast via ::text so
    # the NULL / 'null' / '{}' cases are all caught.
    op.execute(
        f"""
        UPDATE stores
        SET business_hours_json = '{_DEFAULT_HOURS}'::json
        WHERE business_hours_json IS NULL
           OR business_hours_json::text = 'null'
           OR business_hours_json::text = '{{}}'
        """
    )
    op.execute(
        """
        UPDATE stores
        SET timezone = 'Africa/Nairobi'
        WHERE timezone IS NULL OR timezone = ''
        """
    )


def downgrade() -> None:
    # Non-destructive backfill — nothing to undo.
    pass
