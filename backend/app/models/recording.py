"""RecordingClip — one row per camera per rolling recording window.

Written by the recorder service (app/tasks/recorder.py). Completed source
windows are retained for a bounded recovery period before deletion; the row is
kept with status='deleted' and file_path=NULL as an audit trail.
"""
from datetime import datetime

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# recording  — ffmpeg is currently writing this file
# completed  — the window ended and the file is retained for delayed extraction
# deleted    — the file has been purged; file_path is NULL
RECORDING_CLIP_STATUSES = ("recording", "completed", "deleted")


class RecordingClip(Base):
    __tablename__ = "recording_clips"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    camera_id:  Mapped[int]      = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    store_id:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    # e.g. "20260714_0900" — YYYYMMDD_HHMM of the window start (EAT).
    window_id:  Mapped[str]      = mapped_column(String(32), index=True)
    file_path:  Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    ended_at:   Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    file_size_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    status:     Mapped[str]      = mapped_column(String(16), default="recording")


Index("ix_recording_clips_camera_started", RecordingClip.camera_id, RecordingClip.started_at)
