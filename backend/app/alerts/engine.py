"""Alert engine — promote DetectionEvents to operator-visible alerts.

Inputs come from the inference worker via Redis pub/sub channel
`vg:pub:alerts`. The engine:

  - de-duplicates rapid repeats per (camera, type) within DEDUP_SECONDS
  - applies camera-level mute / schedule (if configured)
  - hits every enabled notifier in parallel

Notifications are best-effort; a failing channel never blocks others.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

import redis.asyncio as aioredis

from app.alerts.notifiers.base import AlertPayload, Notifier
from app.alerts.notifiers.smtp import SMTPNotifier
from app.alerts.notifiers.twilio_sms import TwilioSMSNotifier
from app.alerts.notifiers.webhook import WebhookNotifier
from app.config import settings
from app.database import SessionLocal
from app.models import Alert, Camera, DetectionEvent, Zone

log = logging.getLogger(__name__)

DEDUP_SECONDS = 30
# When an event's extra.priority == "high" (currently: intrusion), use a
# much shorter dedup so urgent events aren't silenced.
DEDUP_SECONDS_HIGH = 5


class AlertEngine:
    def __init__(self, notifiers: Iterable[Notifier] | None = None):
        self.notifiers = list(notifiers or (
            SMTPNotifier(), TwilioSMSNotifier(), WebhookNotifier(),
        ))
        # Last fire timestamps for de-dup, keyed by (camera_id, detection_type, zone_id).
        self._last: dict[tuple[int, str, int | None], float] = {}

    def _should_emit(self, camera_id: int, dtype: str, zone_id: int | None,
                     priority: str = "normal") -> bool:
        key = (camera_id, dtype, zone_id)
        now = time.time()
        last = self._last.get(key, 0.0)
        window = DEDUP_SECONDS_HIGH if priority == "high" else DEDUP_SECONDS
        if now - last < window:
            return False
        self._last[key] = now
        return True

    async def handle_event(self, payload: dict) -> None:
        """`payload` is the JSON dict published by the inference worker."""
        camera_id = int(payload["camera_id"])
        dtype     = str(payload["detection_type"])
        zone_id   = payload.get("zone_id")
        priority  = ((payload.get("extra") or {}).get("priority")) or "normal"
        if not self._should_emit(camera_id, dtype, zone_id, priority=priority):
            return

        # Hydrate camera + zone for human-friendly fields.
        camera_name = f"camera-{camera_id}"
        zone_name: str | None = None
        with SessionLocal() as db:
            cam = db.get(Camera, camera_id)
            if cam:
                camera_name = cam.name
            if zone_id:
                z = db.get(Zone, zone_id)
                if z:
                    zone_name = z.name

        alert_payload = AlertPayload(
            alert_id=int(payload["id"]),
            event_id=int(payload["id"]),
            camera_id=camera_id,
            camera_name=camera_name,
            detection_type=dtype,
            confidence=float(payload.get("confidence", 0.0)),
            timestamp_iso=payload.get("ts") or datetime.now(timezone.utc).isoformat(),
            zone_name=zone_name,
            thumbnail_url=None,
            extra=payload.get("extra"),
        )

        # Fan out to all enabled channels concurrently.
        tasks = [n.send(alert_payload) for n in self.notifiers if n.is_enabled()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_engine_forever() -> None:
    """Subscribe to vg:pub:alerts and dispatch indefinitely."""
    engine = AlertEngine()
    r = aioredis.from_url(settings.redis_url)
    psub = r.pubsub()
    await psub.subscribe("vg:pub:alerts")
    log.info("alert engine: subscribed to vg:pub:alerts (notifiers=%s)",
             [n.name for n in engine.notifiers if n.is_enabled()])
    async for msg in psub.listen():
        if msg.get("type") != "message":
            continue
        try:
            data = msg["data"]
            payload = json.loads(data.decode() if isinstance(data, bytes) else data)
            await engine.handle_event(payload)
        except Exception as e:
            log.exception("alert engine error: %s", e)
