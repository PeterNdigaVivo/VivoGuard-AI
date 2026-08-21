"""Alerts — operator-facing surface for events that warrant attention."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# new       — just emitted, not yet seen
# confirmed — operator marked as true positive (feeds back to training)
# dismissed — operator marked as false positive (ditto)
# escalated — sent to higher tier / external system
ALERT_STATUSES = ("new", "confirmed", "dismissed", "escalated")


class Alert(Base):
    __tablename__ = "alerts"

    id:               Mapped[int]   = mapped_column(primary_key=True)
    event_id:         Mapped[int]   = mapped_column(ForeignKey("detection_events.id", ondelete="CASCADE"), unique=True)
    status:           Mapped[str]   = mapped_column(String(16), default="new", index=True)
    assigned_to:      Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    acknowledged_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_used_for_training: Mapped[bool] = mapped_column(Boolean, default=False)
    # Quality-control quarantine never discards the alert or its evidence.
    # It only prevents live/external escalation and automatic learning until
    # an operator has reviewed the unreliable camera/detector pair.
    review_only:      Mapped[bool] = mapped_column(Boolean, default=False,
                                                    server_default="false", index=True)
    training_eligible: Mapped[bool] = mapped_column(Boolean, default=True,
                                                     server_default="true", index=True)
    notification_suppressed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True)
    # Free-text investigation notes the operator adds via "📋 Add Note".
    # Accumulates over time — the UI appends rather than overwrites.
    notes:            Mapped[str | None]    = mapped_column(Text, nullable=True)
    # Set only when the operator hits "✅ Resolved" — distinct from
    # `acknowledged_at` (also bumped on dismiss) so we can report on
    # how quickly real incidents were resolved.
    resolved_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Timeline snapshots for checkout_dwell alerts — one JPEG per
    # 60s from counter-zone arrival to departure. Append-only:
    # alerter writes frames 1..N at fire time; the detector's
    # session-close hook appends N+1..M atomically. NULL for all
    # other alert types and for snapshot-less checkout alerts
    # (e.g. when the camera was offline mid-session). Pruned 24h
    # after `created_at` by alerting.prune_checkout_snapshots.
    snapshot_paths:   Mapped[list | None] = mapped_column(JSON, nullable=True)

    event = relationship("DetectionEvent", back_populates="alert")


class AlertReviewDecision(Base):
    """Append-only reviewer evidence; Alert.status remains the current view."""

    __tablename__ = "alert_review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
