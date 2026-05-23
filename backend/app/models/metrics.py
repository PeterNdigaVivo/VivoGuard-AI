"""Time-series metric snapshots and visitor-tracking dedup.

`MetricSnapshot` is a generic time-bucketed aggregate row used by every
analytics widget in the dashboard:
  queue_length, queue_wait_seconds, occupancy, unique_visitors,
  dwell_seconds, staff_present_pct, passersby, stop_rate, etc.

It is intentionally wide (jsonb `dims`) so we can add new metric types
without schema migrations.

`VisitorTrack` deduplicates entry events into "one unique visitor per
day" buckets. Without re-identification, dedup is keyed by
(store, day, camera, track_id). A future ReID upgrade swaps the key for
a perceptual hash without changing the schema.
"""
from datetime import date as date_t, datetime
from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Canonical metric_type values — kept as plain strings to avoid an enum
# migration every time a new analytics widget is added.
METRIC_TYPES = (
    "occupancy",                 # head count in a zone or store
    "queue_length",              # people in a queue zone
    "queue_wait_seconds",        # tracker dwell time end-to-end
    "unique_visitors",           # daily dedup'd count
    "passersby",                 # window/sidewalk people count
    "stop_rate",                 # passersby who stopped > N seconds / passersby
    "stop_engagement_seconds",   # avg dwell at window
    "dwell_seconds",             # avg dwell time per zone
    "staff_present_pct",         # % of period a counter zone had staff
    "shutter_open",              # 1/0 each sample
    "demographic_bucket",        # count split by age/gender bucket
    "footfall_heatmap_cell",     # 32x32 grid cell traffic accumulator
)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id:           Mapped[int]   = mapped_column(primary_key=True)
    store_id:     Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id:    Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    zone_id:      Mapped[int | None] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"), nullable=True)
    metric_type:  Mapped[str]   = mapped_column(String(48), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end:   Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value:        Mapped[float] = mapped_column(Float)
    # Optional dimensions: {"gender":"f","age_bucket":"25-34"}, {"shift":"morning"}, etc.
    dims:         Mapped[dict | None] = mapped_column(JSON, nullable=True)


Index("ix_metrics_store_type_period",
      MetricSnapshot.store_id, MetricSnapshot.metric_type, MetricSnapshot.period_start.desc())


class VisitorTrack(Base):
    """One row per (store, day, camera, track_id) — the unique-visitor dedup table."""

    __tablename__ = "visitor_tracks"

    id:        Mapped[int]   = mapped_column(primary_key=True)
    store_id:  Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id: Mapped[int]   = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    day:       Mapped[date_t] = mapped_column(Date, index=True)
    track_signature: Mapped[str] = mapped_column(String(128))   # camera_id:track_id (or perceptual hash later)
    first_seen:Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Optional demographic snapshot taken at first-seen.
    demographic_age:    Mapped[str | None] = mapped_column(String(16), nullable=True)
    demographic_gender: Mapped[str | None] = mapped_column(String(8),  nullable=True)


Index("ix_visitor_track_dedup_key",
      VisitorTrack.store_id, VisitorTrack.day, VisitorTrack.track_signature, unique=True)


class StockroomAccess(Base):
    """Audit log of every person entry/exit into a stockroom zone."""

    __tablename__ = "stockroom_accesses"

    id:        Mapped[int]   = mapped_column(primary_key=True)
    store_id:  Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id: Mapped[int]   = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    zone_id:   Mapped[int | None] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"), nullable=True)
    direction: Mapped[str]   = mapped_column(String(8))    # "in" | "out"
    track_id:  Mapped[int]   = mapped_column(Integer)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_authorised: Mapped[bool | None] = mapped_column(nullable=True)   # None=unknown, True/False once classified
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


Index("ix_stockroom_store_ts",
      StockroomAccess.store_id, StockroomAccess.timestamp.desc())


class HeatmapSnapshot(Base):
    """Daily archived heatmap PNG per camera. 30-day rolling retention.

    Snapshot is taken near midnight by `heatmap.snapshot_all` Celery
    beat task. The PNG itself lives on the recordings/data volume.
    """

    __tablename__ = "heatmap_snapshots"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    camera_id:  Mapped[int]      = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    store_id:   Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"),
                                                   nullable=True, index=True)
    day:        Mapped[date_t]   = mapped_column(Date, index=True)
    file_path:  Mapped[str]      = mapped_column(String(512))
    peak_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HeatmapGridSnapshot(Base):
    """Hourly snapshot of one heatmap layer (traffic / engagement /
    congestion / paths) per camera. Drives the replay timeline so
    operators can scrub through a day or compare yesterday vs today.
    90-day retention enforced by the snapshot task.
    """

    __tablename__ = "heatmap_grid_snapshots"

    id:          Mapped[int]      = mapped_column(primary_key=True)
    camera_id:   Mapped[int]      = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    store_id:    Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"),
                                                     nullable=True, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hour:        Mapped[int]      = mapped_column(Integer)
    layer:       Mapped[str]      = mapped_column(String(32))
    grid_data:   Mapped[dict]     = mapped_column(JSON)
    peak_value:  Mapped[float | None] = mapped_column(Float, nullable=True)


class Campaign(Base):
    """Marketing campaign window for before/after analytics."""

    __tablename__ = "campaigns"

    id:        Mapped[int]   = mapped_column(primary_key=True)
    name:      Mapped[str]   = mapped_column(String(128))
    store_id:  Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    start_date:Mapped[date_t] = mapped_column(Date)
    end_date:  Mapped[date_t] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at:Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
