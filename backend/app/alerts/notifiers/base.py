"""Notifier base class + payload shape."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AlertPayload:
    alert_id: int
    event_id: int
    camera_id: int
    camera_name: str
    detection_type: str
    confidence: float
    timestamp_iso: str
    zone_name: str | None = None
    thumbnail_url: str | None = None
    extra: dict | None = None


class Notifier:
    """Subclass and implement `send`. Channels with missing credentials
    should `is_enabled() -> False` so the engine skips them silently."""

    name: str = "base"

    def is_enabled(self) -> bool:
        return False

    async def send(self, alert: AlertPayload) -> None:
        raise NotImplementedError
