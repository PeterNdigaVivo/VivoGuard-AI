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
    resolved_at: datetime | None = None
    notes: str | None = None
    feedback_used_for_training: bool
    created_at: datetime

    # Joined event fields (filled by the route).
    camera_id: int | None = None
    camera_name: str | None = None
    detection_type: str | None = None
    confidence: float | None = None
    bbox_norm: list[float] | None = None
    zone_id: int | None = None
    zone_name: str | None = None
    thumbnail_path: str | None = None

    # Server-rendered presentation fields (May-2026 redesign). The
    # frontend stops guessing severity/labels — it just renders what
    # we send. Keeps the SAME translation logic across the per-store
    # feed AND the chain /alerts page.
    severity: str | None = None        # 'critical' | 'warning' | 'info'
    title:    str | None = None        # "⚠️ Counter Unstaffed — Vivo Runda Cam 3"
    body:     str | None = None        # plain-English description with context
    # URL the browser can GET to fetch a snapshot for this alert.
    # Falls back to the camera's latest cached frame when the event
    # itself doesn't carry a stored thumbnail.
    snapshot_url: str | None = None


class AlertActionOut(BaseModel):
    id: int
    status: str


class AlertNoteIn(BaseModel):
    """Body for POST /alerts/{id}/note. We APPEND to the notes field
    so investigation history isn't lost."""
    note: str
