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
from app.config import settings

log = logging.getLogger(__name__)


class WhatsAppNotifier(Notifier):
    """Disabled — Ops decision (dashboard alerts only). Kept in the
    notifier registry as a no-op so existing dispatch code still
    iterates cleanly; is_enabled() always returns False."""
    name = "whatsapp"

    def is_enabled(self) -> bool:
        return False

    async def send(self, alert: AlertPayload) -> None:
        log.info("WhatsApp disabled — skipping notify for %s",
                 alert.detection_type)
        return
