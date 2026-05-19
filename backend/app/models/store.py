"""Store locations and shift schedules.

A `Store` is a physical retail site (one of ~26 for Vivo Fashion Group).
Cameras can belong to a store via the new `store_id` FK; legacy cameras
that still only have the `site` text label continue to work.

`Shift` rows describe when staff are expected to be present — used by
the Staff Availability detector (P2) and shift-aware metric filtering.
"""
from datetime import datetime, time
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, Time, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Store(Base):
    __tablename__ = "stores"

    id:        Mapped[int]      = mapped_column(primary_key=True)
    name:      Mapped[str]      = mapped_column(String(128), unique=True, index=True)
    code:      Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)  # e.g. "NRB-001"
    country:   Mapped[str]      = mapped_column(String(64))           # "Kenya" | "Uganda" | "Rwanda"
    city:      Mapped[str | None] = mapped_column(String(128), nullable=True)
    address:   Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone:  Mapped[str]      = mapped_column(String(64), default="Africa/Nairobi")

    # business_hours_json keyed by lowercase weekday name. Each entry is
    # a list of "HH:MM-HH:MM" strings so a store can have multiple windows
    # per day (e.g. {"sat": ["09:00-13:00", "14:00-20:00"]}). Empty list
    # = closed that day.
    business_hours_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Soft cap used by occupancy alerts.
    capacity:  Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Manager contact — surfaces in the alerts pipeline (WhatsApp routing).
    manager_name:  Mapped[str | None] = mapped_column(String(128), nullable=True)
    manager_phone: Mapped[str | None] = mapped_column(String(32),  nullable=True)

    # Default RTSP port for cameras added to this store. The Add Camera
    # wizard prefills this when the operator picks a store, so all
    # Moi Ave cameras (Dahua-on-7000) get 7000 by default without
    # re-typing per camera.
    default_rtsp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at:Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shifts  = relationship("Shift", back_populates="store", cascade="all, delete-orphan")


class Shift(Base):
    """Staff shift template. One row per shift definition per day."""

    __tablename__ = "shifts"

    id:        Mapped[int]      = mapped_column(primary_key=True)
    store_id:  Mapped[int]      = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    name:      Mapped[str]      = mapped_column(String(64))            # "morning", "evening"
    day_of_week: Mapped[int]    = mapped_column(Integer)               # 0=Mon … 6=Sun
    start_time:Mapped[time]     = mapped_column(Time)
    end_time:  Mapped[time]     = mapped_column(Time)
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)

    store = relationship("Store", back_populates="shifts")
