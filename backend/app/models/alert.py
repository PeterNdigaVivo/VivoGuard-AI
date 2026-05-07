"""Alerts — operator-facing surface for events that warrant attention."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
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
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    event = relationship("DetectionEvent", back_populates="alert")
