"""Twilio SMS notifier."""
from __future__ import annotations
import logging
import asyncio

from app.alerts.notifiers.base import AlertPayload, Notifier
from app.config import settings

log = logging.getLogger(__name__)


class TwilioSMSNotifier(Notifier):
    name = "twilio"

    def is_enabled(self) -> bool:
        return bool(settings.twilio_account_sid and settings.twilio_auth_token
                    and settings.twilio_from_number)

    async def send(self, alert: AlertPayload) -> None:
        if not self.is_enabled():
            return
        # twilio sdk is sync — run in a thread.
        body = (f"VivoGuard ALERT: {alert.detection_type} on "
                f"{alert.camera_name} ({alert.confidence:.2f}) at {alert.timestamp_iso}")
        # Recipient: read from env at runtime; for now we use SMTP_FROM
        # as the address-book hint by piggybacking on the admin email's
        # local part — production deployments should add a dedicated env.
        to_number = (alert.extra or {}).get("sms_to") or ""
        if not to_number:
            log.debug("twilio: no SMS recipient configured for this alert")
            return

        def _send():
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            client.messages.create(body=body, from_=settings.twilio_from_number, to=to_number)

        try:
            await asyncio.to_thread(_send)
        except Exception as e:
            log.warning("Twilio send failed: %s", e)
