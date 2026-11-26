"""Authoritative incident lifecycle: deduplicated, one action per incident

Revision ID: 0050
Revises: 0049

Until now one promoted detection produced one independently actionable
alert. A person standing in a stockroom for ten minutes could become a
dozen open alerts; the Alerts page grouped them visually, but the backend,
the counts, bulk resolve and the export all still saw a dozen.

This migration turns the dormant 0040 ``incidents`` table into the
operator-facing record:

* lifecycle columns (status, queue, priority, first/last seen, event
  count, policy decision and reason, who closed it and why);
* ``dedup_key`` plus the partial unique index
  ``uq_incidents_open_dedup_key`` — at most one OPEN incident per
  store/camera/zone/detector/rule, enforced by the database so two
  workers racing on the same event cannot both open one;
* ``alerts.incident_id`` so every historical alert row is kept and points
  at the incident it is evidence for.

Foreign keys created by 0040 with ON DELETE RESTRICT are relaxed (camera
SET NULL; member/outbox rows CASCADE with their alert). Cameras are
sometimes removed with a manual DELETE and their history cascades away;
RESTRICT would make that delete fail as soon as incidents exist.

Existing rows (the old one-alert-per-incident shadow projection, present
only where INCIDENT_FOUNDATIONS_ENABLED was turned on) are kept and marked
``queue='legacy'`` with a status copied from their alert. They have no
dedup key, so they can never collide with or be counted as live work.

No data is deleted. Behaviour does not change until
INCIDENT_LIFECYCLE_ENABLED=true is set *and* the runtime compatibility
check (app.services.incidents.lifecycle_ready) sees this schema.

Idempotent guards per house style.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPEN_SQL = "status IN ('open', 'acknowledged', 'escalated')"

_INCIDENT_COLUMNS = (
    ("status", lambda: sa.Column("status", sa.String(16), nullable=False,
                                 server_default="open")),
    ("queue", lambda: sa.Column("queue", sa.String(16), nullable=False,
                                server_default="incident")),
    ("priority", lambda: sa.Column("priority", sa.String(16), nullable=False,
                                   server_default="attention")),
    ("zone_id", lambda: sa.Column("zone_id", sa.Integer(), sa.ForeignKey(
        "zones.id", ondelete="SET NULL", name="fk_incidents_zone_id"),
        nullable=True)),
    ("rule", lambda: sa.Column("rule", sa.String(64), nullable=True)),
    ("dedup_key", lambda: sa.Column("dedup_key", sa.String(255), nullable=True)),
    ("first_seen_at", lambda: sa.Column("first_seen_at",
                                        sa.DateTime(timezone=True), nullable=True)),
    ("last_seen_at", lambda: sa.Column("last_seen_at",
                                       sa.DateTime(timezone=True), nullable=True)),
    ("event_count", lambda: sa.Column("event_count", sa.Integer(),
                                      nullable=False, server_default="1")),
    ("notify_count", lambda: sa.Column("notify_count", sa.Integer(),
                                       nullable=False, server_default="1")),
    ("head_alert_id", lambda: sa.Column("head_alert_id", sa.Integer(),
                                        sa.ForeignKey("alerts.id", ondelete="SET NULL",
                                                      name="fk_incidents_head_alert_id"),
                                        nullable=True)),
    ("policy_decision", lambda: sa.Column("policy_decision", sa.String(16),
                                          nullable=True)),
    ("policy_reason", lambda: sa.Column("policy_reason", sa.String(96),
                                        nullable=True)),
    ("escalated_at", lambda: sa.Column("escalated_at",
                                       sa.DateTime(timezone=True), nullable=True)),
    ("closed_at", lambda: sa.Column("closed_at", sa.DateTime(timezone=True),
                                    nullable=True)),
    ("closed_by", lambda: sa.Column("closed_by", sa.Integer(), sa.ForeignKey(
        "users.id", ondelete="SET NULL", name="fk_incidents_closed_by"),
        nullable=True)),
    ("close_reason", lambda: sa.Column("close_reason", sa.String(64),
                                       nullable=True)),
)


def _inspect(bind):
    from sqlalchemy import inspect as _i
    return _i(bind)


def _columns(bind, table: str) -> set:
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set:
    return {ix["name"] for ix in _inspect(bind).get_indexes(table)}


def _relax_fk(bind, table: str, column: str, ref: str, ondelete: str) -> None:
    """Re-create the 0040 FK on ``table.column`` with a new ON DELETE."""
    if bind.dialect.name != "postgresql":
        return
    for fk in _inspect(bind).get_foreign_keys(table):
        if fk.get("constrained_columns") == [column] and fk.get("name"):
            op.drop_constraint(fk["name"], table, type_="foreignkey")
    op.create_foreign_key(f"fk_{table}_{column}", table, ref, [column], ["id"],
                          ondelete=ondelete)


def upgrade() -> None:
    bind = op.get_bind()

    cols = _columns(bind, "incidents")
    for name, factory in _INCIDENT_COLUMNS:
        if name not in cols:
            op.add_column("incidents", factory())

    # Camera deletions must not be blocked by incident history.
    if bind.dialect.name == "postgresql":
        op.alter_column("incidents", "camera_id", nullable=True)
    _relax_fk(bind, "incidents", "camera_id", "cameras", "SET NULL")
    _relax_fk(bind, "incident_members", "alert_id", "alerts", "CASCADE")
    _relax_fk(bind, "incident_members", "event_id", "detection_events", "CASCADE")
    _relax_fk(bind, "delivery_outbox", "alert_id", "alerts", "CASCADE")

    # Pre-0050 shadow rows: keep, label legacy, copy status from the alert.
    if "status" not in cols:
        op.execute("""
            UPDATE incidents SET
                queue = 'legacy',
                status = COALESCE((
                    SELECT CASE
                        WHEN a.status IN ('dismissed') THEN 'dismissed'
                        WHEN a.status IN ('resolved', 'confirmed') THEN 'resolved'
                        ELSE 'open' END
                    FROM incident_members m JOIN alerts a ON a.id = m.alert_id
                    WHERE m.incident_id = incidents.id
                    ORDER BY m.id LIMIT 1), 'resolved'),
                first_seen_at = opened_at,
                last_seen_at = opened_at
            WHERE dedup_key IS NULL
        """)

    idx = _indexes(bind, "incidents")
    if "uq_incidents_open_dedup_key" not in idx:
        op.create_index("uq_incidents_open_dedup_key", "incidents",
                        ["dedup_key"], unique=True,
                        postgresql_where=sa.text(_OPEN_SQL),
                        sqlite_where=sa.text(_OPEN_SQL))
    if "ix_incidents_queue_status_seen" not in idx:
        op.create_index("ix_incidents_queue_status_seen", "incidents",
                        ["queue", "status", "last_seen_at"])
    for name in ("status", "queue", "priority", "last_seen_at", "closed_at"):
        if f"ix_incidents_{name}" not in idx:
            op.create_index(f"ix_incidents_{name}", "incidents", [name])

    if "incident_id" not in _columns(bind, "alerts"):
        op.add_column("alerts", sa.Column(
            "incident_id", sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL",
                          name="fk_alerts_incident_id"),
            nullable=True))
    if "ix_alerts_incident_id" not in _indexes(bind, "alerts"):
        op.create_index("ix_alerts_incident_id", "alerts", ["incident_id"])


def downgrade() -> None:
    """Drop the lifecycle columns. Incident rows themselves are kept.

    Rolling back loses dedup keys and lifecycle state but never alerts,
    detection events or the 0040 projection rows.
    """
    bind = op.get_bind()
    if "ix_alerts_incident_id" in _indexes(bind, "alerts"):
        op.drop_index("ix_alerts_incident_id", table_name="alerts")
    if "incident_id" in _columns(bind, "alerts"):
        if bind.dialect.name == "postgresql":
            op.drop_constraint("fk_alerts_incident_id", "alerts",
                               type_="foreignkey")
        op.drop_column("alerts", "incident_id")
    idx = _indexes(bind, "incidents")
    for name in ("uq_incidents_open_dedup_key", "ix_incidents_queue_status_seen",
                 "ix_incidents_status", "ix_incidents_queue",
                 "ix_incidents_priority", "ix_incidents_last_seen_at",
                 "ix_incidents_closed_at"):
        if name in idx:
            op.drop_index(name, table_name="incidents")
    cols = _columns(bind, "incidents")
    for name, _ in reversed(_INCIDENT_COLUMNS):
        if name in cols:
            op.drop_column("incidents", name)
