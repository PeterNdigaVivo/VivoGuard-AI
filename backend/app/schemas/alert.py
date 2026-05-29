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
    # Non-technical traffic-light label for store managers:
    # 'URGENT' (red) | 'ATTENTION' (amber) | 'INFO' (blue).
    severity_label: str | None = None
    title:    str | None = None        # "⚠️ Counter Unstaffed — Vivo Runda Cam 3"
    # Plain-English heading with NO camera suffix, for the big card
    # title ("Someone in Store After Hours").
    plain_title: str | None = None
    body:     str | None = None        # plain-English description with context
    # Up to 3 plain-English "what to do" steps for non-technical staff.
    what_to_do: list[str] | None = None
    # Human-readable when-it-happened line. Duration-style events
    # render as "🕒 Between 9:30 PM and 9:55 PM (25 min)"; point-in-
    # time events as "🕒 Detected at 9:30 PM". Always in the camera's
    # store-local timezone.
    time_range: str | None = None
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
