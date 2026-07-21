"""Trained AI models in the registry."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AIModel(Base):
    __tablename__ = "ai_models"

    id:           Mapped[int]   = mapped_column(primary_key=True)
    name:         Mapped[str]   = mapped_column(String(128))
    version:      Mapped[str]   = mapped_column(String(32))      # "v1", "v2", ...
    base_model:   Mapped[str]   = mapped_column(String(32))      # yolov8n|s|m|l|x.pt
    classes_json: Mapped[list]  = mapped_column(JSON)            # ["person", "hardhat", ...]

    # Eval metrics (set when training finishes).
    map50:        Mapped[float | None] = mapped_column(Float, nullable=True)
    map50_95:     Mapped[float | None] = mapped_column(Float, nullable=True)
    precision:    Mapped[float | None] = mapped_column(Float, nullable=True)
    recall:       Mapped[float | None] = mapped_column(Float, nullable=True)

    weights_path:  Mapped[str]  = mapped_column(Text)            # /data/models/...pt or s3://...
    export_format: Mapped[str | None] = mapped_column(String(16), nullable=True)  # pt|onnx|engine

    deployed:     Mapped[bool]  = mapped_column(Boolean, default=False)
    is_base:      Mapped[bool]  = mapped_column(Boolean, default=False)  # bundled YOLOv8 weights
    # Chain-training metadata. is_chain_model=True flags a model
    # trained on pooled samples from every store (see commit 1 of the
    # P5 overhaul). detector_type narrows it to one detector
    # ('uniform', 'shutter'); trained_on_stores lists the store ids
    # whose samples contributed; sample_count is the dataset size.
    is_chain_model:    Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    detector_type:     Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    trained_on_stores: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sample_count:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    training_job_id: Mapped[int | None] = mapped_column(ForeignKey("training_jobs.id", ondelete="SET NULL"), nullable=True)
    # The model this one was incrementally fine-tuned FROM (Part 2 #7). NULL
    # for full retrains from the base yolov8n weights.
    parent_model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True)

    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
