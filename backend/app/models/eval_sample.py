"""Evaluation test-set samples (docs/MODEL_EVALUATION.md).

One row per test image or clip. Files live under settings.eval_dir on the
server (never in git); this table holds the labels and provenance.

kind:
  alert   a reviewed alert: clean frame + the operator's verdict
  people  a busy-hour frame; admins click each visible person
  clip    a short recording; admins count the people in it (tracker tests)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EvalSample(Base):
    __tablename__ = "eval_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # Alert-derived samples keep a link for provenance; SET NULL so the
    # 90-day alert prune never blocks on the test set (which has its own
    # retention, settings.eval_retention_days).
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True, index=True)
    camera_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    detection_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Operator verdict for kind=alert: "true" | "false".
    verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    frame_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    clip_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Zone polygon (normalised [[x,y],...]) and the event's person box, as
    # they were at alert time — what the scorer needs to judge the alert.
    zone_polygon: Mapped[list | None] = mapped_column(JSON, nullable=True)
    event_bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # pending | labelled | skipped. kind=alert is labelled on arrival.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # people: {"points": [[x,y], ...]} normalised; clip: {"count": n}.
    label: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    labelled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
