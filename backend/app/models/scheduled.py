"""Customer journey persistence."""
from datetime import date as date_t, datetime
from sqlalchemy import (
    Date, DateTime, ForeignKey, JSON, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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
