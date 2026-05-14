"""Scheduled reports (P2) and customer journeys (P3)."""
from datetime import date as date_t, datetime
from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, JSON, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduledReport(Base):
    __tablename__ = "scheduled_reports"

    id:         Mapped[int]   = mapped_column(primary_key=True)
    name:       Mapped[str]   = mapped_column(String(128))
    store_id:   Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True)
    cadence:    Mapped[str]   = mapped_column(String(16), default="daily")     # daily | weekly
    format:     Mapped[str]   = mapped_column(String(8),  default="pdf")       # pdf  | csv
    recipients: Mapped[str]   = mapped_column(Text)                            # CSV list
    last_run_at:Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active:  Mapped[bool]  = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerJourney(Base):
    __tablename__ = "customer_journeys"

    id:         Mapped[int]   = mapped_column(primary_key=True)
    store_id:   Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id:  Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True)
    day:        Mapped[date_t] = mapped_column(Date, index=True)
    track_signature: Mapped[str] = mapped_column(String(128))
    zone_sequence_json: Mapped[list] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
