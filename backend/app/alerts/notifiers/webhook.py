"""Generic outbound webhook notifier (POST JSON)."""
from __future__ import annotations
import logging
from dataclasses import asdict

import httpx

from app.alerts.notifiers.base import AlertPayload, Notifier
from app.config import settings

log = logging.getLogger(__name__)


class WebhookNotifier(Notifier):
    name = "webhook"

    def is_enabled(self) -> bool:
        return bool(settings.webhook_url)

    async def send(self, alert: AlertPayload) -> None:
        if not self.is_enabled():
            return
        headers = {"Content-Type": "application/json"}
        if settings.webhook_auth_header:
            headers["Authorization"] = settings.webhook_auth_header
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(settings.webhook_url, json=asdict(alert), headers=headers)
        except Exception as e:
            log.warning("webhook send failed: %s", e)
