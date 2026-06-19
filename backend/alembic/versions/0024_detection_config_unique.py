"""detection_configs: dedupe + UNIQUE(camera_id, detection_type)

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-19

detection_configs never had a uniqueness guard on
(camera_id, detection_type). Migration 0022's backfill used
`ON CONFLICT DO NOTHING` which — without a constraint to conflict
on — relied purely on row-absence, so a camera could (and in
production did) accumulate duplicate config rows for the same
detection type. Duplicates are a correctness hazard: the inference
worker reads the config via `.first()`, so a stale disabled
duplicate could silently win and leave a detector off.

upgrade() runs in two ordered steps so it never fails on a dirty
table:
  1. Delete duplicate rows — for every (camera_id, detection_type)
     group with >1 row, keep the highest id (most recent) and
     delete the rest.
  2. Add the UNIQUE constraint, guarded by a DO block so re-running
     on an already-constrained DB is a no-op (IF NOT EXISTS pattern;
     Postgres has no `ADD CONSTRAINT IF NOT EXISTS` literal).

Once the constraint exists, 0022's `ON CONFLICT DO NOTHING` and any
future upsert gain a real arbiter.

downgrade() drops the constraint only — it does NOT recreate
duplicates (data deletion is intentionally irreversible).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "uq_detection_configs__camera_type"


def upgrade() -> None:
    # 1) Dedupe BEFORE constraining so the ALTER can't fail. Deletes
    #    every row that has a sibling with the same
    #    (camera_id, detection_type) and a higher id → keeps max(id)
    #    per group.
    op.execute(
        """
        DELETE FROM detection_configs a
              USING detection_configs b
              WHERE a.camera_id      = b.camera_id
                AND a.detection_type = b.detection_type
                AND a.id < b.id
        """
    )
    # 2) Add the UNIQUE constraint idempotently. Postgres lacks
    #    `ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS`, so guard with
    #    a catalogue check.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{_CONSTRAINT}'
            ) THEN
                ALTER TABLE detection_configs
                  ADD CONSTRAINT {_CONSTRAINT}
                  UNIQUE (camera_id, detection_type);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Constraint only — the dedupe is not reversed.
    op.execute(
        f"ALTER TABLE detection_configs DROP CONSTRAINT IF EXISTS {_CONSTRAINT}"
    )
