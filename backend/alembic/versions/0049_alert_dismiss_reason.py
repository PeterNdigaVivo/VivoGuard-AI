"""Structured reason on a dismissed alert

Revision ID: 0049
Revises: 0048

Operators dismiss alerts constantly and almost never say why: 509
dismissals in the fortnight to 2026-10-26 carried 4 notes between them.
That is not carelessness, it is a rational response to free text while
watching 21 stores - typing costs more than the click is worth.

Every learning mechanism we want (feeding corrections back into the VLM
prompt, calibrating thresholds, learning per-camera suppression rules)
needs to know WHY something was wrong. Today that information is
destroyed at the moment it is created.

So the reason becomes one tap from a short fixed list rather than a text
box. A column, not a free-text note, because the whole point is that it
can be counted and grouped.

Nullable on purpose: a dismissal without a reason is still a valid
dismissal, and the operator must never be blocked from clearing their
feed.

Idempotent guards per house style.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0049"
down_revision: Union[str, None] = "0048"
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
    if "dismiss_reason" not in cols:
        op.add_column("alerts", sa.Column("dismiss_reason", sa.String(32),
                                          nullable=True))
    # The query this exists to serve is "what reasons has camera X been
    # dismissed for lately", which the prompt-memory loop runs per camera
    # on every sweep.
    if "ix_alerts__dismiss_reason" not in _indexes(bind, "alerts"):
        op.create_index("ix_alerts__dismiss_reason", "alerts",
                        ["dismiss_reason", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_alerts__dismiss_reason" in _indexes(bind, "alerts"):
        op.drop_index("ix_alerts__dismiss_reason", table_name="alerts")
    if "dismiss_reason" in _columns(bind, "alerts"):
        op.drop_column("alerts", "dismiss_reason")
