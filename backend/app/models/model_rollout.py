"""Model rollouts (canary -> manual promotion) and recorded evaluations."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelRollout(Base):
    __tablename__ = "model_rollouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("ai_models.id", ondelete="CASCADE"), index=True)
    # canary | promoted | rolled_back
    stage: Mapped[str] = mapped_column(String(16), default="canary", index=True)
    camera_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    # {camera_id: previous ai_model_id or null} so rollback is exact.
    previous_assignment_json: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ModelEvaluation(Base):
    """One recorded evaluation: validation (training split), held_out
    (fixed evaluation set), or canary (live reviewed verdicts)."""
    __tablename__ = "model_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("ai_models.id", ondelete="CASCADE"), index=True)
    rollout_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_rollouts.id", ondelete="CASCADE"), nullable=True, index=True)
    split: Mapped[str] = mapped_column(String(16), index=True)
    precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    false_positive_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-detector / per-class breakdown and anything split-specific.
    metrics_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reasons_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
