"""Per-camera detection configuration (one row per detection type)."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# Canonical detection types — the UI toggles enable/disable these.
DETECTION_TYPES = (
    "person", "vehicle", "animal", "lpr", "face",
    "weapon", "weapon_brandished", "crowd",
    "trespass", "tripwire", "tailgating", "loitering",
    "fall", "smoke", "fire", "abandoned_object",
    "shelf", "occupancy", "heatmap", "custom",
    # Retail extension (commit 1+).
    "queue", "unique_visitor", "intrusion",
    # P2 features.
    "staff_present", "dwell", "shutter",
    # P3 features.
    "uniform_compliance", "demographic", "shrinkage", "stockroom_access",
    # P4 features.
    "passersby", "window_engagement",
    # Lumana parity.
    "entry_exit", "fight", "shelf_change",
    # Vivo P5 — staff-only behind-counter compliance.
    "staff_zone",
    # Vivo P6 — per-customer transaction duration at counter zones.
    "checkout_dwell",
)


class DetectionConfig(Base):
    __tablename__ = "detection_configs"

    id:               Mapped[int]   = mapped_column(primary_key=True)
    camera_id:        Mapped[int]   = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    detection_type:   Mapped[str]   = mapped_column(String(32))  # see DETECTION_TYPES
    enabled:          Mapped[bool]  = mapped_column(Boolean, default=False)

    # Common knobs.
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.6)
    min_object_size:  Mapped[int]   = mapped_column(Integer, default=0)        # px²
    detection_every_n_frames: Mapped[int] = mapped_column(Integer, default=1)  # 1 = every frame

    # Type-specific knobs (see UI: dwell time, crowd N, tripwire line coords).
    dwell_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crowd_threshold:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra:            Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Schedule: {"mon":[["08:00","18:00"]], ..., "holidays":["2026-01-01"]}.
    schedule_json:    Mapped[dict | None] = mapped_column(JSON, nullable=True)

    updated_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    camera = relationship("Camera", back_populates="detection_configs")
