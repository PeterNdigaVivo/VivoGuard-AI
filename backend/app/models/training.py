"""Training Studio: datasets, images, annotations, training jobs."""
from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Dataset(Base):
    __tablename__ = "datasets"

    id:          Mapped[int]   = mapped_column(primary_key=True)
    name:        Mapped[str]   = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    classes_json:Mapped[list]  = mapped_column(JSON, default=list)
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    images = relationship("TrainingImage", back_populates="dataset", cascade="all, delete-orphan")


class TrainingImage(Base):
    __tablename__ = "training_images"

    id:          Mapped[int]   = mapped_column(primary_key=True)
    dataset_id:  Mapped[int]   = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    camera_id:   Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True)
    file_path:   Mapped[str]   = mapped_column(Text)             # local path or s3 key
    width:       Mapped[int | None] = mapped_column(Integer, nullable=True)
    height:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labeled:     Mapped[bool]  = mapped_column(Boolean, default=False)
    # split: "train" | "val" | "test" — populated when training job starts.
    split:       Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    dataset     = relationship("Dataset", back_populates="images")
    annotations = relationship("Annotation", back_populates="image", cascade="all, delete-orphan")


class Annotation(Base):
    __tablename__ = "annotations"

    id:           Mapped[int]   = mapped_column(primary_key=True)
    image_id:     Mapped[int]   = mapped_column(ForeignKey("training_images.id", ondelete="CASCADE"), index=True)
    class_label:  Mapped[str]   = mapped_column(String(64))
    # bbox_json: [cx, cy, w, h] normalised (YOLO format).
    bbox_json:    Mapped[list]  = mapped_column(JSON)
    annotated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified:     Mapped[bool]  = mapped_column(Boolean, default=False)
    auto_suggested: Mapped[bool]= mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    image = relationship("TrainingImage", back_populates="annotations")


class TrainingSample(Base):
    """A single labelled frame for a CLASSIFICATION detector (one
    label per whole frame, no bounding box). Used by the shutter /
    door-status collection workflow where the operator captures a
    frame and tags it OPEN / CLOSED / PARTIAL.

    Distinct from TrainingImage/Annotation which are for bbox object
    detection — classification needs neither boxes nor per-object
    classes, just frame → label.
    """

    __tablename__ = "training_samples"

    id:            Mapped[int]   = mapped_column(primary_key=True)
    detector_type: Mapped[str]   = mapped_column(String(32), index=True)  # e.g. "shutter"
    label:         Mapped[str]   = mapped_column(String(32), index=True)  # open|closed|partial
    camera_id:     Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    store_id:      Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    frame_path:    Mapped[str]   = mapped_column(Text)
    captured_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    labeled_by:    Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # How the sample arrived in the dataset. 'capture' = grabbed live
    # from a camera; 'upload' = uploaded by an operator from their
    # phone/computer; 'auto' = the every-10-min auto-capture loop.
    source:        Mapped[str]   = mapped_column(String(16), default="capture",
                                                  server_default="capture")
    # When True the sample is part of the chain-wide shared dataset
    # (commit 2). Default True — opting in is the desirable behaviour.
    shared:        Mapped[bool]  = mapped_column(Boolean, default=True, server_default="true")
    # Quality-control gate (commit 3). NULL = pending, True = approved,
    # False = rejected. Only approved samples feed the chain trainer.
    approved:      Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    approved_by:   Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrainingJob(Base):
    __tablename__ = "training_jobs"

    id:           Mapped[int]   = mapped_column(primary_key=True)
    model_name:   Mapped[str]   = mapped_column(String(128))
    dataset_id:   Mapped[int]   = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    config_json:  Mapped[dict]  = mapped_column(JSON)            # base_model, epochs, batch, imgsz, lr, splits, augment toggles
    status:       Mapped[str]   = mapped_column(String(16), default="queued")  # queued|running|done|failed|cancelled
    current_epoch:Mapped[int]   = mapped_column(Integer, default=0)
    total_epochs: Mapped[int]   = mapped_column(Integer, default=0)
    best_map50:   Mapped[float | None] = mapped_column(Float, nullable=True)
    log_path:     Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message:Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
