import asyncio

from app.alerts.notifiers.whatsapp import WhatsAppNotifier
from app.alerts.notifiers.base import AlertPayload
from app.alerts.whatsapp_delivery import normalize_recipient
from app.config import settings


def _alert(priority: str = "high") -> AlertPayload:
    return AlertPayload(
        alert_id=1,
        event_id=2,
        camera_id=3,
        camera_name="Test camera",
        detection_type="intrusion",
        confidence=0.91,
        timestamp_iso="2026-09-06T12:00:00Z",
        extra={"priority": priority},
    )


def test_whatsapp_recipient_normalization():
    assert normalize_recipient("0712345678") == "whatsapp:+254712345678"
    assert normalize_recipient("+254712345678") == "whatsapp:+254712345678"
    assert normalize_recipient("whatsapp:+254712345678") == "whatsapp:+254712345678"


def test_whatsapp_enables_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "sid")
    monkeypatch.setattr(settings, "twilio_auth_token", "token")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "whatsapp:+10000000000")
    monkeypatch.setattr(settings, "whatsapp_to", "whatsapp:+254700000000")
    assert WhatsAppNotifier().is_enabled()


def test_whatsapp_priority_gate(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "sid")
    monkeypatch.setattr(settings, "twilio_auth_token", "token")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "whatsapp:+10000000000")
    monkeypatch.setattr(settings, "whatsapp_to", "whatsapp:+254700000000")
    monkeypatch.setattr(settings, "whatsapp_priority_only", True)
    sent = []
    monkeypatch.setattr(
        "app.alerts.notifiers.whatsapp.send_whatsapp",
        lambda recipients, body: sent.append((recipients, body)) or 1,
    )

    asyncio.run(WhatsAppNotifier().send(_alert("info")))
    assert not sent
    asyncio.run(WhatsAppNotifier().send(_alert("high")))
    assert len(sent) == 1
