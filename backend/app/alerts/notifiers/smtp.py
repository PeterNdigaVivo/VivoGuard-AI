"""SMTP email notifier (aiosmtplib)."""
from __future__ import annotations
import logging
from email.message import EmailMessage

import aiosmtplib

from app.alerts.notifiers.base import AlertPayload, Notifier
from app.config import settings

log = logging.getLogger(__name__)


class SMTPNotifier(Notifier):
    name = "smtp"

    def is_enabled(self) -> bool:
        return bool(settings.smtp_host and settings.smtp_from)

    async def send(self, alert: AlertPayload) -> None:
        if not self.is_enabled():
            return
        # TO list: configurable per deployment; here we send to BOOTSTRAP_ADMIN_EMAIL.
        to = settings.bootstrap_admin_email
        msg = EmailMessage()
        msg["From"] = settings.smtp_from
        msg["To"]   = to
        msg["Subject"] = f"[VivoGuard] {alert.detection_type} — {alert.camera_name}"
        msg.set_content(
            f"Camera:    {alert.camera_name} (id={alert.camera_id})\n"
            f"Type:      {alert.detection_type}\n"
            f"Confidence:{alert.confidence:.2f}\n"
            f"Time:      {alert.timestamp_iso}\n"
            f"Zone:      {alert.zone_name or '-'}\n"
            f"Extra:     {alert.extra or {}}\n"
        )
        try:
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_use_tls,
                timeout=15,
            )
        except Exception as e:
            log.warning("SMTP send failed: %s", e)
