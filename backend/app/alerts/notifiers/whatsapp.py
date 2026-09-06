"""WhatsApp notifier via Twilio Business API.

Reuses the existing Twilio credentials (TWILIO_ACCOUNT_SID +
TWILIO_AUTH_TOKEN). New env vars:
  TWILIO_WHATSAPP_FROM   e.g. "whatsapp:+14155238886" (Twilio sandbox)
  WHATSAPP_TO            comma-separated recipient list, each as
                         "whatsapp:+254712345678"
  WHATSAPP_PRIORITY_ONLY "true" | "false" — when true (default), only
                         alerts with extra.priority == "high" are sent.
                         Stops the manager's phone from melting.
"""
from __future__ import annotations
import asyncio
import logging

from app.alerts.notifiers.base import AlertPayload, Notifier
from app.alerts.whatsapp_delivery import configured, send_whatsapp
from app.config import settings

log = logging.getLogger(__name__)


class WhatsAppNotifier(Notifier):
    name = "whatsapp"

    def is_enabled(self) -> bool:
        return configured() and bool(settings.whatsapp_to.strip())

    async def send(self, alert: AlertPayload) -> None:
        if not self.is_enabled():
            return
        priority = str((alert.extra or {}).get("priority") or "normal").lower()
        if settings.whatsapp_priority_only and priority not in {"high", "urgent"}:
            return

        recipients = settings.whatsapp_to.split(",")
        body = (
            f"VivoGuard {priority.upper()} ALERT\n"
            f"{alert.detection_type} — {alert.camera_name}\n"
            f"Confidence: {alert.confidence:.0%}\n"
            f"Time: {alert.timestamp_iso}"
        )
        sent = await asyncio.to_thread(send_whatsapp, recipients, body)
        log.info("WhatsApp alert %s delivered to %d recipient(s)",
                 alert.alert_id, sent)
