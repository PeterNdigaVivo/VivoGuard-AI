"""Training evidence review: one click is evidence, not a training label

Revision ID: 0054
Revises: 0053

Operator verdicts were stamped review_state='approved' and
eligible_for_training=true the moment they were clicked (migration 0045's
single-reviewer policy), and each click re-ran the retrain trigger. A
single mis-click could therefore reach a model.

This migration:

* adds training_images.example_kind (positive | negative | uncertain |
  policy_error) and training_images.target_class (the object class the
  example teaches, NULL when it is not trainable as an object class);
* flips the column server defaults to review_state='pending_review',
  eligible_for_training=false, so any writer that forgets provenance
  lands in the review queue rather than in a model;
* moves EXISTING operator-click feedback that was auto-approved back to
  pending_review. Nothing is deleted; each moved row keeps its previous
  state in source_extra (pre_0054_review_state / pre_0054_eligible) and
  the downgrade restores it exactly. Curated uploads, simulation evidence
  and independently reviewed rows are untouched.

Consequence to plan for: feedback-driven training pauses until a reviewer
approves enough balanced examples in the evidence queue.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OBJECT_CLASS_FOR_TYPE = {
    "person": "person", "fire": "fire", "smoke": "smoke", "weapon": "weapon",
    "weapon_brandished": "weapon", "vehicle": "vehicle", "animal": "animal",
    "abandoned_object": "abandoned_object",
}
PERSON_FAMILY = {"person", "intrusion", "trespass", "staff_zone", "loitering",
                 "live_activity", "tailgating", "stockroom_access"}


def _columns(bind, table):
    from sqlalchemy import inspect
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _indexes(bind, table):
    from sqlalchemy import inspect
    return {i["name"] for i in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "training_images")
    if "example_kind" not in cols:
        op.add_column("training_images",
                      sa.Column("example_kind", sa.String(16), nullable=True))
    if "target_class" not in cols:
        op.add_column("training_images",
                      sa.Column("target_class", sa.String(64), nullable=True))
    idx = _indexes(bind, "training_images")
    for name in ("example_kind", "target_class"):
        if f"ix_training_images_{name}" not in idx:
            op.create_index(f"ix_training_images_{name}", "training_images", [name])

    op.alter_column("training_images", "review_state",
                    server_default="pending_review")
    op.alter_column("training_images", "eligible_for_training",
                    server_default=sa.text("false"))

    if bind.dialect.name != "postgresql":
        return
    # Auto-approved operator clicks go back to review; remember the old state.
    op.execute("""
        UPDATE training_images SET
            source_extra = (COALESCE(source_extra::jsonb, '{}'::jsonb)
                || jsonb_build_object('pre_0054_review_state', review_state,
                                      'pre_0054_eligible', eligible_for_training))::json,
            review_state = 'pending_review',
            eligible_for_training = false
        WHERE source_kind IN ('operator_confirmed', 'operator_dismissed')
          AND review_state = 'approved'
    """)
    # Classify existing feedback rows.
    rows = bind.execute(sa.text("""
        SELECT ti.id, ti.source_kind, d.name,
               (ti.source_extra::jsonb ->> 'detection_type') AS dtype,
               EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = ti.id) AS boxed
        FROM training_images ti JOIN datasets d ON d.id = ti.dataset_id
        WHERE ti.source_kind IN ('operator_confirmed', 'operator_dismissed')
          AND ti.example_kind IS NULL
    """)).fetchall()
    for image_id, source_kind, ds_name, dtype, boxed in rows:
        name = ds_name or ""
        dtype = dtype or name.replace("feedback-negative-", "").replace("feedback-", "")
        if source_kind == "operator_confirmed" and boxed:
            kind = "positive"
            target = OBJECT_CLASS_FOR_TYPE.get(dtype)
            state = None if target else "excluded"
        else:
            # Historic dismissals carry no reason: a reviewer decides.
            kind = "uncertain"
            target = "person" if dtype in PERSON_FAMILY else OBJECT_CLASS_FOR_TYPE.get(dtype)
            state = None
        bind.execute(sa.text(
            "UPDATE training_images SET example_kind = :k, target_class = :t"
            + (", review_state = :s" if state else "")
            + " WHERE id = :i"),
            {"k": kind, "t": target, "s": state, "i": image_id})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("""
            UPDATE training_images SET
                review_state = source_extra::jsonb ->> 'pre_0054_review_state',
                eligible_for_training =
                    (source_extra::jsonb ->> 'pre_0054_eligible')::boolean,
                source_extra = (source_extra::jsonb
                    - 'pre_0054_review_state' - 'pre_0054_eligible')::json
            WHERE source_extra IS NOT NULL
              AND (source_extra::jsonb) ? 'pre_0054_review_state'
        """)
    op.alter_column("training_images", "review_state", server_default="approved")
    op.alter_column("training_images", "eligible_for_training",
                    server_default=sa.text("true"))
    idx = _indexes(bind, "training_images")
    for name in ("example_kind", "target_class"):
        if f"ix_training_images_{name}" in idx:
            op.drop_index(f"ix_training_images_{name}", table_name="training_images")
    cols = _columns(bind, "training_images")
    for name in ("target_class", "example_kind"):
        if name in cols:
            op.drop_column("training_images", name)
