"""Staff classifier output — one row per (store, day, track) once a
track has been seen for the first time that day.

The row's `classified_as` field is the answer to "is this track a
staff member or a customer?". It's bumped from 'customer' (default)
to 'staff' by the periodic staff-classifier celery task once the
track has spent >10 minutes inside a counter zone.

Analytics endpoints LEFT-JOIN visitor_tracks → staff_tracks and
exclude `classified_as = 'staff'` from visitor counts, dwell
averages, journey aggregates etc.
"""
from datetime import date as date_t, datetime
from sqlalchemy import (
    Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StaffTrack(Base):
    __tablename__ = "staff_tracks"

    id:         Mapped[int]   = mapped_column(primary_key=True)
    store_id:   Mapped[int]   = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    day:        Mapped[date_t] = mapped_column(Date, index=True)
    track_signature: Mapped[str] = mapped_column(String(128))

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen:  Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Seconds the track has accumulated inside any counter zone.
    # >600s flips classified_as to 'staff'.
    counter_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    classified_as:   Mapped[str] = mapped_column(String(16), default="customer",
                                                  server_default="customer")
    # How this track was identified as staff: 'zone' (>10 min in a
    # counter zone — the classifier) or 'uniform' (the uniform detector
    # saw a compliant uniform). Lets the dashboard show both counts.
    source:          Mapped[str] = mapped_column(String(16), default="zone",
                                                  server_default="zone")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    __table_args__ = (
        UniqueConstraint("store_id", "day", "track_signature",
                         name="uq_staff_tracks_store_day_sig"),
    )
