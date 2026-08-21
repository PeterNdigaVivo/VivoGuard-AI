"""Additive incident lifecycle, evidence and delivery-outbox models.

These tables intentionally do not replace ``alerts``.  They form a
feature-flagged projection that can be populated in shadow mode while the
existing alert path remains the production source of truth.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


INCIDENT_STATES = (
    "provisional", "verified", "downgraded", "retracted", "expired",
)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    camera_id: Mapped[int] = mapped_column(
        ForeignKey("cameras.id", ondelete="RESTRICT"), index=True)
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


class IncidentMember(Base):
    __tablename__ = "incident_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="RESTRICT"), unique=True, index=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("detection_events.id", ondelete="RESTRICT"), unique=True, index=True)
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
        ForeignKey("alerts.id", ondelete="RESTRICT"), index=True)
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
