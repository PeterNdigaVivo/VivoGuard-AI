"""Read-only Odoo integration projections.

Only operational identifiers and aggregates are persisted.  Customer names,
employee names, contact details and transaction line items are deliberately
excluded from this boundary.
"""
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Time,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OdooStoreMap(Base):
    __tablename__ = "odoo_store_map"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), unique=True, index=True)
    odoo_model: Mapped[str] = mapped_column(String(64), default="pos.config")
    odoo_res_id: Mapped[int] = mapped_column(Integer, index=True)
    odoo_pos_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Nairobi")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint("odoo_model", "odoo_res_id", name="uq_odoo_store_model_res"),
    )


class StoreBusinessHours(Base):
    __tablename__ = "store_business_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer)  # 0=Mon ... 6=Sun
    open_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    close_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    source: Mapped[str] = mapped_column(String(24), default="manual")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "day_of_week", "source", name="uq_store_hours_day_source"),
    )


class OdooRosterWindow(Base):
    __tablename__ = "odoo_roster_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    work_day: Mapped[date] = mapped_column(Date, index=True)
    employee_ref: Mapped[str] = mapped_column(String(64))
    shift_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "work_day", "employee_ref", name="uq_odoo_roster_window"),
    )


class OdooPosSession(Base):
    __tablename__ = "odoo_pos_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    odoo_session_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    odoo_config_id: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(24), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OdooTillConflict(Base):
    __tablename__ = "odoo_till_conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    business_day: Mapped[date] = mapped_column(Date, index=True)
    conflict_type: Mapped[str] = mapped_column(String(48), index=True)
    camera_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    till_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "business_day", "conflict_type", name="uq_odoo_till_conflict"),
    )


class OdooStoreSalesHourly(Base):
    __tablename__ = "odoo_store_sales_hourly"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    amount_total: Mapped[float] = mapped_column(Float, default=0.0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "period_start", name="uq_odoo_sales_store_hour"),
    )


class OdooPosActivityBucket(Base):
    """One-minute, store-level count; no order/customer identifiers."""

    __tablename__ = "odoo_pos_activity_buckets"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "period_start", name="uq_odoo_pos_activity_bucket"),
    )


class OdooConversionMetric(Base):
    __tablename__ = "odoo_conversion_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    footfall: Mapped[int] = mapped_column(Integer)
    transactions: Mapped[int] = mapped_column(Integer)
    conversion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "period_start", name="uq_odoo_conversion_store_hour"),
    )


class OdooSyncState(Base):
    __tablename__ = "odoo_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


Index("ix_odoo_roster_store_start_end", OdooRosterWindow.store_id,
      OdooRosterWindow.shift_start, OdooRosterWindow.shift_end)
