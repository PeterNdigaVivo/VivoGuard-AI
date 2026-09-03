"""Restore single-reviewer training eligibility (policy switch)

Revision ID: 0045
Revises: 0044

Migration 0042 quarantined every training image lacking TWO independent
same-verdict reviewer decisions, and the feedback loop began stamping
all new operator clicks eligible_for_training=false / review_state=
'pending'. Vivo operates a single-reviewer workflow, so this starved
training to zero: cross-store jobs 807/808/809 failed at prep with
"insufficient validation images: 0 < 5 (train=0, test=0)" and the
pseudo-labeler stopped labelling (its selection also requires
eligibility).

This migration re-approves OPERATOR feedback only:
  1. rows 0042 quarantined (marked legacy_independent_review_required)
  2. rows the post-0038 feedback loop stamped pending
     (source_kind operator_confirmed / operator_dismissed)
Synthetic / simulation / mined provenance quarantines are untouched.

Honours the deployment policy: set TRAINING_REQUIRE_DUAL_REVIEW=true
in the environment to keep the dual-review gate — the migration then
becomes a no-op (and stays reversible via downgrade either way).
"""
from __future__ import annotations

import os

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def _dual_review_required() -> bool:
    return os.environ.get("TRAINING_REQUIRE_DUAL_REVIEW", "").strip().lower() \
        in {"1", "true", "yes"}


def upgrade() -> None:
    if _dual_review_required():
        return
    # 1) Reverse 0042's legacy quarantine (its own downgrade shape).
    op.execute("""
        UPDATE training_images AS image
        SET source_kind = 'operator_verified',
            eligible_for_training = true,
            review_state = 'approved',
            source_extra = (
                COALESCE(image.source_extra::jsonb, '{}'::jsonb)
                - 'legacy_independent_review_required'
                - 'legacy_source_kind'
                || jsonb_build_object('single_reviewer_policy', true)
            )::json
        WHERE image.eligible_for_training = false
          AND image.review_state = 'pending'
          AND COALESCE(image.source_extra::jsonb, '{}'::jsonb)
              @> '{"legacy_independent_review_required": true}'::jsonb
    """)
    # 2) Approve operator feedback stamped pending by the post-0038
    #    feedback loop (confirmed positives AND dismissed negatives —
    #    both are operator decisions under the single-reviewer policy).
    op.execute("""
        UPDATE training_images AS image
        SET eligible_for_training = true,
            review_state = 'approved',
            source_extra = (
                COALESCE(image.source_extra::jsonb, '{}'::jsonb)
                || jsonb_build_object('single_reviewer_policy', true)
            )::json
        WHERE image.eligible_for_training = false
          AND image.review_state = 'pending'
          AND image.source_kind IN ('operator_confirmed',
                                    'operator_dismissed')
    """)


def downgrade() -> None:
    # Re-quarantine exactly the rows this migration approved.
    op.execute("""
        UPDATE training_images AS image
        SET eligible_for_training = false,
            review_state = 'pending',
            source_extra = (
                COALESCE(image.source_extra::jsonb, '{}'::jsonb)
                - 'single_reviewer_policy'
            )::json
        WHERE COALESCE(image.source_extra::jsonb, '{}'::jsonb)
              @> '{"single_reviewer_policy": true}'::jsonb
    """)
