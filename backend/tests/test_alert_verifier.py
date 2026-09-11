"""Alert verifier: annotate, never hide.

Real ORM models on in-memory SQLite; the Anthropic SDK is replaced by
a fake module so no network is touched. Pins the three acceptance
behaviours: JSON parse failure degrades to uncertain (never raises),
missing images still verify, and the verdict NEVER mutates
review_only / notification_suppressed / status / severity inputs.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Alert, AlertReviewDecision, Camera, DetectionEvent, Store

TABLES = [Store.__table__, Camera.__table__, DetectionEvent.__table__,
          Alert.__table__, AlertReviewDecision.__table__]


def _fake_anthropic_module() -> types.ModuleType:
    """A stand-in anthropic SDK. Set mod.reply (str or Exception)
    per test; the client reads it at call time."""
    mod = types.ModuleType("anthropic")
    mod.reply = "{}"

    class _Messages:
        def create(self, **kwargs):
            if isinstance(mod.reply, Exception):
                raise mod.reply
            return SimpleNamespace(
                content=[SimpleNamespace(text=mod.reply)])

    class _Client:
        def __init__(self, api_key=None, timeout=None):
            self.messages = _Messages()

    mod.Anthropic = _Client
    mod.APIConnectionError = ConnectionError
    mod.APITimeoutError = TimeoutError
    return mod


@pytest.fixture()
def db(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = _fake_anthropic_module()
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=TABLES)
    maker = sessionmaker(bind=eng)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    s = maker()
    yield s, fake, tmp_path
    s.close()


def _seed_alert(s, tmp_path, *, with_thumb=True, detection_type="intrusion"):
    store = s.query(Store).filter(Store.name == "Vivo Yaya").first()
    if store is None:
        store = Store(name="Vivo Yaya", country="Kenya")
        s.add(store); s.flush()
    cam = Camera(name="Entrance", store_id=store.id, brand="dahua",
                 connection_type="rtsp", host="10.0.0.9")
    s.add(cam); s.flush()
    thumb = None
    if with_thumb:
        p = tmp_path / "snap.jpg"
        p.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
        thumb = str(p)
    ev = DetectionEvent(camera_id=cam.id, detection_type=detection_type,
                        confidence=0.9, bbox_json=[0.1, 0.1, 0.5, 0.5],
                        timestamp=datetime.now(timezone.utc),
                        thumbnail_path=thumb)
    s.add(ev); s.flush()
    a = Alert(event_id=ev.id, status="new")
    s.add(a); s.commit()
    return a


def test_json_parse_failure_marks_uncertain_never_raises(db):
    s, fake, tmp_path = db
    a = _seed_alert(s, tmp_path)
    fake.reply = "sorry, I cannot answer in JSON today"
    from app.services.alert_verifier import verify
    verify(a.id)                       # must not raise
    s.expire_all()
    row = s.get(Alert, a.id)
    assert row.ai_verdict == "uncertain"
    assert row.ai_confidence == 0.0
    assert row.ai_verified_at is not None
    assert row.ai_reason and len(row.ai_reason) <= 120


def test_missing_images_still_verifies(db):
    s, fake, tmp_path = db
    a = _seed_alert(s, tmp_path, with_thumb=False)
    fake.reply = (
        '{"verdict": "false_alert", "confidence": 1.7, '
        '"likely_outcome": "Nothing happens.", '
        '"recommended_action": "Ignore it.", '
        '"reason": "No frames were available."}')
    from app.services.alert_verifier import verify
    verify(a.id)
    s.expire_all()
    row = s.get(Alert, a.id)
    assert row.ai_verdict == "false_alert"
    assert row.ai_confidence == 1.0    # clamped to [0, 1]
    assert row.ai_action == "Ignore it."
    assert row.ai_verified_at is not None


def test_verdict_never_mutates_operational_fields(db):
    s, fake, tmp_path = db
    a = _seed_alert(s, tmp_path)
    a.review_only = True               # pre-existing quality flags stay put
    a.notification_suppressed = True
    s.commit()
    fake.reply = (
        '{"verdict": "false_alert", "confidence": 0.99, '
        '"likely_outcome": "x", "recommended_action": "y", "reason": "z"}')
    from app.services.alert_verifier import verify
    verify(a.id)
    s.expire_all()
    row = s.get(Alert, a.id)
    assert row.ai_verdict == "false_alert"
    # The verdict is annotation ONLY: nothing operational moved.
    assert row.review_only is True
    assert row.notification_suppressed is True
    assert row.status == "new"
    assert row.training_eligible is True
    # And the inverse: flags that started False stay False.
    b = _seed_alert(s, tmp_path)
    verify(b.id)
    s.expire_all()
    row_b = s.get(Alert, b.id)
    assert row_b.review_only is False
    assert row_b.notification_suppressed is False


def test_non_visual_types_skip_the_api_entirely(db):
    s, fake, tmp_path = db
    a = _seed_alert(s, tmp_path, detection_type="store_intelligence")
    # If the API were called, this would raise and the reason would be
    # the error string instead of the non-visual marker.
    fake.reply = RuntimeError("API must not be called for non-visual types")
    from app.services.alert_verifier import verify
    verify(a.id)
    s.expire_all()
    row = s.get(Alert, a.id)
    assert row.ai_verdict == "uncertain"
    assert row.ai_confidence == 0.0
    assert row.ai_reason == "non-visual alert type"
    assert row.ai_verified_at is not None
    assert row.ai_model == "none"
