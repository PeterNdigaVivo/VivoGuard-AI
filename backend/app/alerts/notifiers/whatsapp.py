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
    name = "whatsapp"

    def is_enabled(self) -> bool:
        return bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and getattr(settings, "twilio_whatsapp_from", "")
            and getattr(settings, "whatsapp_to", "")
        )

    def _priority_only(self) -> bool:
        return str(getattr(settings, "whatsapp_priority_only", "true")).lower() != "false"

    async def send(self, alert: AlertPayload) -> None:
        if not self.is_enabled():
            return
        priority = (alert.extra or {}).get("priority", "normal")
        if self._priority_only() and priority != "high":
            return

        body = (
            f"🚨 VivoGuard *{alert.detection_type}*\n"
            f"Camera: {alert.camera_name}\n"
            f"Confidence: {alert.confidence:.2f}\n"
            f"Time: {alert.timestamp_iso}\n"
            f"{('Zone: ' + alert.zone_name + chr(10)) if alert.zone_name else ''}"
        )
        recipients = [r.strip() for r in settings.whatsapp_to.split(",") if r.strip()]

        def _send():
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            for to in recipients:
                client.messages.create(
                    body=body,
                    from_=settings.twilio_whatsapp_from,
                    to=to,
                )

        try:
            await asyncio.to_thread(_send)
        except Exception as e:
            log.warning("WhatsApp send failed: %s", e)
