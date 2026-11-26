"""Typed contracts for the /incidents API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.alert import AlertOut

IncidentView = Literal["open", "attention", "review", "resolved", "all"]
IncidentAction = Literal["acknowledge", "resolve", "dismiss", "escalate"]


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    queue: str
    priority: str
    detection_type: str
    rule: Optional[str] = None
    store_id: Optional[int] = None
    store_name: Optional[str] = None
    camera_id: Optional[int] = None
    camera_name: Optional[str] = None
    zone_id: Optional[int] = None
    zone_name: Optional[str] = None
    opened_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    escalated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    event_count: int = 1
    notify_count: int = 1
    policy_decision: Optional[str] = None
    policy_reason: Optional[str] = None
    version: int = 1
    # The first alert of the incident: title, picture, clip, guidance.
    head: Optional[AlertOut] = None


class IncidentPage(BaseModel):
    view: IncidentView
    total: int
    limit: int
    offset: int
    items: list[IncidentOut]


class IncidentTimelineEntry(BaseModel):
    alert_id: int
    event_id: int
    at: Optional[datetime] = None
    confidence: Optional[float] = None
    snapshot_url: Optional[str] = None
    notification_suppressed: bool = False


class IncidentTransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_state: Optional[str] = None
    to_state: str
    actor_type: str
    actor_id: Optional[str] = None
    reason_code: str
    created_at: Optional[datetime] = None


class IncidentDetail(IncidentOut):
    timeline: list[IncidentTimelineEntry] = []
    transitions: list[IncidentTransitionOut] = []


class IncidentSummary(BaseModel):
    urgent: int
    attention: int
    review: int
    resolved: int
    open: int
    all: int
    avg_response_seconds: Optional[int] = None
    lifecycle_enabled: bool


class IncidentActionIn(BaseModel):
    reason: Optional[str] = None
    note: Optional[str] = None
    # Only for resolve: "true" records that the detector was right.
    # dismiss always records "false". Neither trains a model directly;
    # verdicts enter training only through the review queue.
    verdict: Optional[Literal["true"]] = None
    version: Optional[int] = None


class BulkResolveIn(BaseModel):
    view: IncidentView = "open"
    store_id: Optional[int] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    # The number the operator was shown and confirmed. If the server
    # finds a different number, nothing is changed (HTTP 409).
    expected_count: int


class BulkResolveOut(BaseModel):
    resolved: int
    ids: list[int]
