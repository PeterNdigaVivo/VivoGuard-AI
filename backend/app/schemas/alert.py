"""Pydantic schemas for the alerts surface."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int
    status: str
    assigned_to: int | None
    acknowledged_at: datetime | None
    feedback_used_for_training: bool
    created_at: datetime

    # Joined event fields (filled by the route).
    camera_id: int | None = None
    camera_name: str | None = None
    detection_type: str | None = None
    confidence: float | None = None
    bbox_norm: list[float] | None = None
    zone_id: int | None = None
    thumbnail_path: str | None = None


class AlertActionOut(BaseModel):
    id: int
    status: str
