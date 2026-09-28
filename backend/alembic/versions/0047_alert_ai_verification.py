"""AI verification columns on alerts (annotate, never hide)

Revision ID: 0047
Revises: 0046

Adds the seven ai_* columns the alert verifier writes NEXT TO each
alert, an (ai_verdict, created_at desc) index for the feed filter, and
alert_review_decisions.extra so operator verdicts can record the AI
verdict that was showing at decision time (verifier precision data).

The verifier never suppresses, hides, reclassifies, downgrades or
delays an alert; these columns are annotation only.
Idempotent guards per house style.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {ix["name"] for ix in _inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "alerts")
    for name, type_ in (
        ("ai_verdict",     sa.String(16)),
        ("ai_confidence",  sa.Float()),
        ("ai_outcome",     sa.Text()),
        ("ai_action",      sa.Text()),
        ("ai_reason",      sa.Text()),
        ("ai_verified_at", sa.DateTime(timezone=True)),
        ("ai_model",       sa.String(64)),
    ):
        if name not in cols:
            op.add_column("alerts", sa.Column(name, type_, nullable=True))
    if "ix_alerts__ai_verdict_created" not in _indexes(bind, "alerts"):
        op.create_index("ix_alerts__ai_verdict_created", "alerts",
                        ["ai_verdict", sa.text("created_at DESC")])
    if "extra" not in _columns(bind, "alert_review_decisions"):
        op.add_column("alert_review_decisions",
                      sa.Column("extra", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_alerts__ai_verdict_created" in _indexes(bind, "alerts"):
        op.drop_index("ix_alerts__ai_verdict_created", table_name="alerts")
    cols = _columns(bind, "alerts")
    for name in ("ai_model", "ai_verified_at", "ai_reason", "ai_action",
                 "ai_outcome", "ai_confidence", "ai_verdict"):
        if name in cols:
            op.drop_column("alerts", name)
    if "extra" in _columns(bind, "alert_review_decisions"):
        op.drop_column("alert_review_decisions", "extra")
