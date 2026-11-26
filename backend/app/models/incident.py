"""Incident lifecycle, evidence and delivery-outbox models.

An Incident is the single operator-actionable record for one continuing
event. Alerts are still written, one per promoted detection, for audit and
backward compatibility; every alert belonging to a continuing event points
at the same Incident through ``alerts.incident_id`` and an
``incident_members`` row (the incident timeline).

Deduplication is enforced by the database, not by Redis TTLs or browser
grouping: ``uq_incidents_open_dedup_key`` is a partial unique index that
allows at most one OPEN incident per dedup key (store, camera, zone,
detector, rule). See ``app.services.incidents``.

Rows created before migration 0053 (the old one-alert-per-incident shadow
projection) carry ``queue='legacy'`` and no dedup key, so they can never
collide with, or be counted as, live incidents.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


INCIDENT_STATES = (
    "provisional", "verified", "downgraded", "retracted", "expired",
)

# Operational lifecycle (what the operator does), distinct from the
# evaluation state above (whether the evidence held up).
INCIDENT_OPEN_STATUSES = ("open", "acknowledged", "escalated")
INCIDENT_CLOSED_STATUSES = ("resolved", "dismissed")
INCIDENT_STATUSES = INCIDENT_OPEN_STATUSES + INCIDENT_CLOSED_STATUSES
# incident: actionable; review: human review, no urgent notification;
# legacy: pre-0053 shadow rows, never shown as live work.
INCIDENT_QUEUES = ("incident", "review", "legacy")
INCIDENT_PRIORITIES = ("urgent", "attention", "low")
_OPEN_SQL = "status IN ('open', 'acknowledged', 'escalated')"


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("uq_incidents_open_dedup_key", "dedup_key", unique=True,
              postgresql_where=text(_OPEN_SQL), sqlite_where=text(_OPEN_SQL)),
        Index("ix_incidents_queue_status_seen", "queue", "status",
              "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    detection_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    evaluation_state: Mapped[str] = mapped_column(
        String(24), default="provisional", server_default="provisional", index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # --- Lifecycle (migration 0053) ---------------------------------
    status: Mapped[str] = mapped_column(
        String(16), default="open", server_default="open", index=True)
    queue: Mapped[str] = mapped_column(
        String(16), default="incident", server_default="incident", index=True)
    priority: Mapped[str] = mapped_column(
        String(16), default="attention", server_default="attention", index=True)
    zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("zones.id", ondelete="SET NULL"), nullable=True)
    rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    event_count: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1")
    notify_count: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1")
    head_alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alerts.id", ondelete="SET NULL", use_alter=True,
                   name="fk_incidents_head_alert_id"), nullable=True)
    policy_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    policy_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IncidentMember(Base):
    __tablename__ = "incident_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), unique=True, index=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("detection_events.id", ondelete="CASCADE"), unique=True, index=True)
    source_event_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class IncidentTransition(Base):
    __tablename__ = "incident_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str] = mapped_column(String(24), index=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)


class EvidenceManifest(Base):
    __tablename__ = "evidence_manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), unique=True, index=True)
    snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    clip_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    filmstrip_paths_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Eligibility and availability are deliberately separate. At alert
    # creation eligibility is commonly unknown until recording/codec/retention
    # checks finish; treating "no clip yet" as "ineligible" would corrupt the
    # evidence-SLA denominator.
    clip_eligible: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, index=True)
    clip_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True)
    ineligible_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clip_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DeliveryOutbox(Base):
    __tablename__ = "delivery_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    destination_ref: Mapped[str] = mapped_column(String(255))
    payload_version: Mapped[str] = mapped_column(String(16), default="1.0")
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(24), default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
