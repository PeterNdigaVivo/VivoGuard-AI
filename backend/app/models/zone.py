"""Zones drawn on the camera view (polygons or tripwire lines)."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Zone(Base):
    __tablename__ = "zones"

    id:           Mapped[int]   = mapped_column(primary_key=True)
    camera_id:    Mapped[int]   = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    name:         Mapped[str]   = mapped_column(String(128))

    # "polygon" (closed area) or "line" (tripwire); stored coords are in
    # *normalised* image space [0..1] so the zone scales with resolution.
    shape:        Mapped[str]   = mapped_column(String(16), default="polygon")
    polygon_coords_json: Mapped[list] = mapped_column(JSON)  # [[x,y], [x,y], ...]

    # Detection types this zone applies to (subset of DetectionConfig types).
    detection_types_json: Mapped[list] = mapped_column(JSON, default=list)

    # When this zone is *active* (same shape as DetectionConfig.schedule_json).
    active_schedule_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    suppressed:   Mapped[bool]  = mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    camera = relationship("Camera", back_populates="zones")
