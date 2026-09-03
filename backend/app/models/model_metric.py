"""ModelMetric — daily precision / FP-rate per (model, detection_type).

Populated by the `training.compute_model_metrics_daily` Celery task
from operator-marked alerts. One row per (model_id, detection_type, day).

precision = TP / (TP + FP)
fp_rate   = FP / (TP + FP) for operator-triaged alerts
recall    = reserved for shadow-eval (Phase 3); operator marks don't
            give us a count of missed detections.
"""
from datetime import date, datetime

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelMetric(Base):
    __tablename__ = "model_metrics"
    __table_args__ = (
        UniqueConstraint("model_id", "detection_type", "day",
                         name="uq_model_metrics__model_type_day"),
    )

    id:             Mapped[int]   = mapped_column(primary_key=True)
    model_id:       Mapped[int | None] = mapped_column(
        ForeignKey("ai_models.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    detection_type: Mapped[str]   = mapped_column(String(32), index=True)
    day:            Mapped[date]  = mapped_column(Date, index=True)
    tp_count:       Mapped[int]   = mapped_column(Integer, default=0,
                                                   server_default="0")
    fp_count:       Mapped[int]   = mapped_column(Integer, default=0,
                                                   server_default="0")
    alerts_total:   Mapped[int]   = mapped_column(Integer, default=0,
                                                   server_default="0")
    precision:      Mapped[float | None] = mapped_column(Float, nullable=True)
    fp_rate:        Mapped[float | None] = mapped_column(Float, nullable=True)
    recall:         Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at:    Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
