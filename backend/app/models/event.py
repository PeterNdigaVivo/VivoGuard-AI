"""DetectionEvent — every AI hit that crosses thresholds is recorded here.

Promoting an event to an Alert is decided by the alert engine
(noise filtering, schedule, zone rules, dedup).
"""
from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DetectionEvent(Base):
    __tablename__ = "detection_events"

    id:             Mapped[int]   = mapped_column(primary_key=True)
    camera_id:      Mapped[int]   = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    zone_id:        Mapped[int | None] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"), nullable=True)
    detection_type: Mapped[str]   = mapped_column(String(32), index=True)
    confidence:     Mapped[float] = mapped_column(Float)
    # bbox_json: [x1, y1, x2, y2] in normalised image coords [0..1].
    bbox_json:      Mapped[list]  = mapped_column(JSON)
    # Free-form payload (e.g., LPR plate text, crowd count, tracker id).
    extra:          Mapped[dict | None] = mapped_column(JSON, nullable=True)

    timestamp:      Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    clip_path:      Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id:       Mapped[int | None] = mapped_column(ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True)
    reviewed:       Mapped[bool]  = mapped_column(default=False)

    camera = relationship("Camera", back_populates="events")
    alert  = relationship("Alert",  back_populates="event", uselist=False)


# Composite index for the alerts page's "by camera, recent" query.
Index("ix_detection_events_camera_ts", DetectionEvent.camera_id, DetectionEvent.timestamp.desc())
