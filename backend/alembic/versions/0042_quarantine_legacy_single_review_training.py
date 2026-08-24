"""quarantine legacy training evidence without independent agreement

Revision ID: 0042
Revises: 0041
"""
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE training_images AS image
        SET source_kind = 'operator_confirmed',
            eligible_for_training = false,
            review_state = 'pending',
            source_extra = (
                COALESCE(image.source_extra::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'legacy_independent_review_required', true,
                    'legacy_source_kind', image.source_kind
                )
            )::json
        WHERE image.source_kind = 'operator_verified'
          AND image.eligible_for_training = true
          AND (
              image.source_alert_id IS NULL
              OR (
                  SELECT COUNT(DISTINCT decision.reviewer_id)
                  FROM alert_review_decisions AS decision
                  WHERE decision.alert_id = image.source_alert_id
              ) < 2
              OR (
                  SELECT COUNT(DISTINCT decision.verdict)
                  FROM alert_review_decisions AS decision
                  WHERE decision.alert_id = image.source_alert_id
              ) <> 1
          )
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE training_images AS image
        SET source_kind = 'operator_verified',
            eligible_for_training = true,
            review_state = 'approved',
            source_extra = (
                COALESCE(image.source_extra::jsonb, '{}'::jsonb)
                - 'legacy_independent_review_required'
                - 'legacy_source_kind'
            )::json
        WHERE image.source_kind = 'operator_confirmed'
          AND image.eligible_for_training = false
          AND image.review_state = 'pending'
          AND COALESCE(image.source_extra::jsonb, '{}'::jsonb)
              @> '{"legacy_independent_review_required": true}'::jsonb
    """)
