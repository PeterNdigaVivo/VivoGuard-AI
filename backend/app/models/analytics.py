"""Analytics event tables — discrete, queryable records that back
integrations (Odoo sales attribution) rather than aggregate KPIs.

ZoneVisit rows are written by the HeatmapDetector's counter-presence
logger in app/ai/detectors/stateful.py: one row per occupancy episode
of a `counter`-tagged zone (entered_at when the zone goes occupied by
a non-staff person, exited_at + duration when it empties for 5s).
SalesAICorrelation rows are the audit trail of the Odoo sale-confirmed
webhook: one row per received sale, recording whether camera evidence
of a customer at the counter existed around the sale time.
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ZoneVisit(Base):
    __tablename__ = "zone_visits"

    id:         Mapped[int] = mapped_column(primary_key=True)
    store_id:   Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    camera_id:  Mapped[int] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    zone_id:    Mapped[int] = mapped_column(
        ForeignKey("zones.id", ondelete="CASCADE"), index=True)
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True)
    # NULL while the zone is still occupied (visit in progress, or a
    # slice died mid-visit; the next slice closes danglers).
    exited_at:  Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True)


class SalesAICorrelation(Base):
    """Audit log for the Odoo sale-confirmed webhook: every received
    sale and whether a counter-zone visit overlapped it."""
    __tablename__ = "sales_ai_correlation"

    id:              Mapped[int] = mapped_column(primary_key=True)
    store_id:        Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True,
        index=True)
    order_id:        Mapped[int] = mapped_column(Integer, index=True)
    order_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True))
    amount_total:    Mapped[float | None] = mapped_column(
        Float, nullable=True)
    # "validated" (a visit overlapped the sale) | "unmatched".
    status:          Mapped[str] = mapped_column(String(16), index=True)
    zone_visit_id:   Mapped[int | None] = mapped_column(
        ForeignKey("zone_visits.id", ondelete="SET NULL"), nullable=True)
    reason:          Mapped[str | None] = mapped_column(
        String(64), nullable=True)
    created_at:      Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
