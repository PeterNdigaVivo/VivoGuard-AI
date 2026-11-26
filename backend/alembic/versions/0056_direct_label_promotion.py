"""Direct label promotion: provenance columns, one label row per alert

Revision ID: 0056
Revises: 0055

Operators mark True/False once on the Alerts page; with
TRAINING_DIRECT_PROMOTION_ENABLED on, that mark becomes the training label
after a trust/agreement gate, a burst guard and an audit sample
(app.services.label_promotion). This migration adds what that needs:

* training_images.label_alert_id (UNIQUE) — the single training row for an
  alert's operator label, so a re-mark updates instead of duplicating.
  Backfilled for existing operator rows (earliest row per alert, never a
  clone or harvested context frame);
* labelled_by_user_id, labelled_at, promotion_path, gate_json,
  audit_sample — who labelled it and how it reached (or did not reach)
  training;
* users.trusted_labeller.

Additive only. With the switch off, behaviour is unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    from sqlalchemy import inspect
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _idx(bind, table):
    from sqlalchemy import inspect
    insp = inspect(bind)
    return ({i["name"] for i in insp.get_indexes(table)}
            | {u["name"] for u in insp.get_unique_constraints(table)})


def upgrade() -> None:
    bind = op.get_bind()
    cols = _cols(bind, "training_images")
    add = [
        ("label_alert_id", sa.Column(
            "label_alert_id", sa.Integer(),
            sa.ForeignKey("alerts.id", ondelete="SET NULL",
                          name="fk_training_images_label_alert_id"),
            nullable=True)),
        ("labelled_by_user_id", sa.Column(
            "labelled_by_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL",
                          name="fk_training_images_labelled_by"),
            nullable=True)),
        ("labelled_at", sa.Column("labelled_at", sa.DateTime(timezone=True),
                                  nullable=True)),
        ("promotion_path", sa.Column("promotion_path", sa.String(32),
                                     nullable=True)),
        ("gate_json", sa.Column("gate_json", sa.JSON(), nullable=True)),
        ("audit_sample", sa.Column("audit_sample", sa.Boolean(), nullable=False,
                                   server_default=sa.text("false"))),
    ]
    for name, col in add:
        if name not in cols:
            op.add_column("training_images", col)
    idx = _idx(bind, "training_images")
    if "uq_training_images_label_alert_id" not in idx:
        op.create_unique_constraint("uq_training_images_label_alert_id",
                                    "training_images", ["label_alert_id"])
    for name in ("labelled_by_user_id", "labelled_at", "promotion_path",
                 "audit_sample"):
        if f"ix_training_images_{name}" not in idx:
            op.create_index(f"ix_training_images_{name}", "training_images", [name])

    if "trusted_labeller" not in _cols(bind, "users"):
        op.add_column("users", sa.Column("trusted_labeller", sa.Boolean(),
                                         nullable=False,
                                         server_default=sa.text("false")))

    if bind.dialect.name == "postgresql":
        # One label row per alert for existing operator feedback: the
        # earliest row, never a clone (store specialist / cross-store) or a
        # harvested context frame. Only rows whose alert still exists.
        op.execute("""
            UPDATE training_images t
               SET label_alert_id = f.source_alert_id,
                   promotion_path = COALESCE(t.promotion_path, 'legacy')
              FROM (SELECT DISTINCT ON (ti.source_alert_id)
                           ti.id, ti.source_alert_id
                      FROM training_images ti
                      JOIN alerts a ON a.id = ti.source_alert_id
                     WHERE ti.source_kind IN ('operator_confirmed', 'operator_dismissed')
                       AND COALESCE(ti.source_extra::jsonb ->> 'harvest', '') = ''
                       AND COALESCE(ti.source_extra::jsonb ->> 'source', '') <> 'cross_store'
                     ORDER BY ti.source_alert_id, ti.id) f
             WHERE t.id = f.id AND t.label_alert_id IS NULL
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if "trusted_labeller" in _cols(bind, "users"):
        op.drop_column("users", "trusted_labeller")
    idx = _idx(bind, "training_images")
    for name in ("labelled_by_user_id", "labelled_at", "promotion_path",
                 "audit_sample"):
        if f"ix_training_images_{name}" in idx:
            op.drop_index(f"ix_training_images_{name}", table_name="training_images")
    if "uq_training_images_label_alert_id" in idx:
        op.drop_constraint("uq_training_images_label_alert_id", "training_images",
                           type_="unique")
    cols = _cols(bind, "training_images")
    for name in ("audit_sample", "gate_json", "promotion_path", "labelled_at",
                 "labelled_by_user_id", "label_alert_id"):
        if name in cols:
            op.drop_column("training_images", name)
