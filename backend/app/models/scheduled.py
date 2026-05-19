"""Scheduled reports (P2) and customer journeys (P3)."""
from datetime import date as date_t, datetime, time as time_t
from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, Time, func,
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
    recipients: Mapped[str]   = mapped_column(Text)                            # CSV email list

    # Cron-precise dispatch (May-2026). NULL keeps the legacy
    # "fire anytime after cadence-hours have elapsed" behaviour;
    # set to e.g. time(21, 0) for "fire at 21:00 store-local".
    time_of_day: Mapped[time_t | None] = mapped_column(Time, nullable=True)
    # Only meaningful for cadence='weekly'. 0=Mon..6=Sun.
    day_of_week: Mapped[int | None]    = mapped_column(Integer, nullable=True)
    # Bumped to today's store-local date on each fire. Prevents the
    # 5-minute beat tick from firing the same report twice inside the
    # operator's fire window.
    last_fire_date: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    # Optional WhatsApp delivery alongside email. Comma-separated list
    # of `whatsapp:+<msisdn>` numbers (Twilio format).
    whatsapp_recipients: Mapped[str | None] = mapped_column(Text, nullable=True)

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
