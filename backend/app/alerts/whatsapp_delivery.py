"""Small synchronous Twilio WhatsApp delivery helper."""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger(__name__)


def normalize_recipient(phone: str | None) -> str | None:
    if not phone:
        return None
    value = phone.strip()
    if value.startswith("whatsapp:"):
        return value
    if not value.startswith("+"):
        value = "+254" + value[1:] if value.startswith("0") else "+" + value
    return f"whatsapp:{value}"


def configured() -> bool:
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_whatsapp_from
    )


def send_whatsapp(recipients: list[str], body: str) -> int:
    """Send once per unique valid recipient and return the success count."""
    if not configured():
        log.warning("WhatsApp delivery skipped: Twilio credentials/from missing")
        return 0

    targets = list(dict.fromkeys(filter(None, map(normalize_recipient, recipients))))
    if not targets:
        log.warning("WhatsApp delivery skipped: no valid recipients")
        return 0

    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    sender = normalize_recipient(settings.twilio_whatsapp_from)
    sent = 0
    for target in targets:
        try:
            client.messages.create(
                body=body,
                from_=sender,
                to=target,
            )
            sent += 1
        except Exception as exc:
            log.warning("WhatsApp send failed for %s: %s", target, exc)
    return sent
