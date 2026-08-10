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
    # Operator-review crop with an orange bbox overlay drawn on it.
    # Best-effort sibling of file_path written at harvest time. NEVER
    # fed to YOLO — drawing a box on training data teaches the model
    # to detect orange boxes, not uniforms. Used by future "browse
    # training pool" UI; the /sprint UI shows live alert snapshots
    # (orange painted via the snapshot `boxes` payload).
    preview_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    width:       Mapped[int | None] = mapped_column(Integer, nullable=True)
    height:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labeled:     Mapped[bool]  = mapped_column(Boolean, default=False)
    # split: "train" | "val" | "test" — populated when training job starts.
    split:       Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Full evidence payload from the originating alert (when the row
    # came from the feedback loop). Mirrors DetectionEvent.extra plus
    # the operator-marked context: detection_type, store_id, zone_id,
    # confidence, bbox, person_count, tracking_ids, alert_id,
    # timestamp_iso, store_name, camera_name. Lets dataset curators
    # dedup + filter without re-joining back to the alert row.
    source_extra:Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # FK to the alert whose verdict (confirm/dismiss) created this
    # row. Lets feedback_loop.revert_verdict() find + delete this
    # image when the operator undoes a sprint label. SET NULL on
    # alert deletion — the image survives a retention prune; the
    # NULL just means we can no longer trace back to the alert.
    source_alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alerts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
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
    # Operator-review crop with orange bbox overlay (preview-pair
    # mirror of TrainingImage.preview_path). Never fed to the chain
    # classifier — orange would become a learned class. Best-effort.
    preview_path:  Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # Dispatch priority (lower = sooner). Part 2 #6. Default 5 = "others".
    priority:     Mapped[int]   = mapped_column(Integer, default=5, server_default="5")
    current_epoch:Mapped[int]   = mapped_column(Integer, default=0)
    total_epochs: Mapped[int]   = mapped_column(Integer, default=0)
    best_map50:   Mapped[float | None] = mapped_column(Float, nullable=True)
    log_path:     Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message:Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Stall watchdog (migration 0035): heartbeat bumped by the trainer at
    # job start and after every epoch; the dispatcher requeues running
    # jobs whose heartbeat is older than training_stall_timeout_minutes.
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Celery task id captured at dispatch so the watchdog can revoke
    # (terminate) the actual task before requeueing.
    celery_task_id:   Mapped[str | None] = mapped_column(String(64), nullable=True)
