"""data: enable entry_exit DetectionConfig for every camera that has an entry_exit zone

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-17

Backfills the runtime gate that the EntryExitDetector consults
before it does anything: `detection_configs WHERE detection_type=
'entry_exit' AND enabled=true`. Cameras that had an entry_exit zone
drawn via the legacy zone-creation flow (which didn't always run
`_autoenable_detection_types`) were sitting with `enabled=false`,
logging "EntryExit DISABLED" forever — cameras 66, 86, 225, 226
were the visible examples.

Idempotent: ON CONFLICT clause flips existing rows to enabled=true,
inserts only missing ones. New zones created via /cameras/{id}/zones/
by-purpose already trigger _autoenable_detection_types, so this is
a one-shot backfill, not a recurring task.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # detection_types_json is a JSON list column. Cast to text + LIKE
    # is the most portable way to ask "does the list contain the
    # entry_exit string?" without relying on jsonb operators that the
    # ORM doesn't surface uniformly.
    op.execute("""
        INSERT INTO detection_configs (camera_id, detection_type, enabled,
                                       confidence_threshold, min_object_size,
                                       detection_every_n_frames)
        SELECT DISTINCT z.camera_id, 'entry_exit', TRUE, 0.6, 0, 1
          FROM zones z
         WHERE z.detection_types_json::text LIKE '%entry_exit%'
        ON CONFLICT DO NOTHING
    """)
    # Separately flip any existing rows that were sitting at
    # enabled=false. (The ON CONFLICT above is no-op for matches —
    # `detection_configs` doesn't have a UNIQUE on
    # (camera_id, detection_type), so ON CONFLICT DO NOTHING relies
    # on the row not existing; this UPDATE handles the disabled
    # duplicates case observed in production.)
    op.execute("""
        UPDATE detection_configs
           SET enabled = TRUE
         WHERE detection_type = 'entry_exit'
           AND enabled = FALSE
           AND camera_id IN (
               SELECT DISTINCT z.camera_id FROM zones z
                WHERE z.detection_types_json::text LIKE '%entry_exit%'
           )
    """)


def downgrade() -> None:
    # Data-only migration; no schema to drop. Down would re-disable
    # configs we deliberately just enabled — leave as a no-op so an
    # accidental rollback can't silence detectors.
    pass
