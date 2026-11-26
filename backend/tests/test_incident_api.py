"""/incidents API: counts, list, export and bulk resolve agree."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.incidents import router
from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user
from app.models import Alert, Camera, DetectionEvent, Store, User
from app.models.incident import Incident
from app.services import alert_policy as policy
from app.services import incidents as svc

T0 = datetime.now(timezone.utc) - timedelta(minutes=30)


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "incident_lifecycle_enabled", True)
    svc._reset_schema_cache()
    import app.services.alert_feedback as fb
    monkeypatch.setattr(fb, "_enqueue_absorb", lambda *a, **k: None)
    with Session() as db:
        store = Store(name="Garden City", country="Kenya", timezone="Africa/Nairobi")
        db.add(store); db.flush()
        db.add(Camera(name="Door", brand="generic", connection_type="lan_rtsp",
                      host="127.0.0.1", store_id=store.id))
        db.add(User(email="op@vivo.test", password_hash="x", role="operator"))
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def _db():
        with Session() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    with Session() as s:
        user = s.query(User).one()
        s.expunge(user)
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), Session
    svc._reset_schema_cache()


def _make(Session, dtype, priority, decision=policy.INCIDENT, n=1, rule=None):
    with Session() as db:
        for i in range(n):
            ev = DetectionEvent(camera_id=1, detection_type=dtype, confidence=0.9,
                                bbox_json=[0, 0, 1, 1], extra={"rule": rule or dtype},
                                timestamp=T0 + timedelta(seconds=i))
            db.add(ev); db.flush()
            a = Alert(event_id=ev.id, status="new")
            db.add(a); db.flush()
            svc.attach_detection(db, a, ev, policy.PolicyDecision(decision, "t", priority),
                                 store_id=1, now=T0 + timedelta(seconds=i))
        db.commit()


def test_counts_match_lists_for_every_view(env):
    client, Session = env
    _make(Session, "fire", "urgent", n=3)                 # one incident, 3 events
    _make(Session, "loitering", "attention")
    _make(Session, "scene_review", "low", decision=policy.REVIEW)
    since = (T0 - timedelta(hours=1)).isoformat()
    s = client.get("/incidents/summary", params={"since": since}).json()
    assert (s["urgent"], s["attention"], s["review"], s["open"]) == (1, 1, 1, 2)
    assert s["lifecycle_enabled"] is True
    for view, key in (("open", "open"), ("attention", "attention"),
                      ("review", "review"), ("resolved", "resolved"), ("all", "all")):
        page = client.get("/incidents", params={"view": view, "since": since}).json()
        assert page["total"] == s[key], view
        assert len(page["items"]) == s[key], view
    first = client.get("/incidents", params={"view": "open"}).json()["items"][0]
    assert first["priority"] == "urgent" and first["event_count"] == 3
    assert first["head"]["id"] is not None


def test_action_closes_incident_and_records_one_verdict(env):
    client, Session = env
    _make(Session, "loitering", "attention", n=4)
    inc_id = client.get("/incidents").json()["items"][0]["id"]
    r = client.post(f"/incidents/{inc_id}/dismiss",
                    json={"reason": "staff_member", "note": "cleaner"})
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    with Session() as db:
        from app.models import AlertReviewDecision
        assert db.query(AlertReviewDecision).count() == 1     # one, not four
        assert {a.status for a in db.query(Alert)} == {"dismissed"}
    again = client.post(f"/incidents/{inc_id}/resolve", json={})
    assert again.status_code == 409


def test_bulk_resolve_requires_the_confirmed_count(env):
    client, Session = env
    _make(Session, "fire", "urgent")
    _make(Session, "loitering", "attention")
    body = {"view": "open", "expected_count": 3}
    r = client.post("/incidents/bulk-resolve", json=body)
    assert r.status_code == 409
    assert client.get("/incidents/summary").json()["open"] == 2
    r = client.post("/incidents/bulk-resolve", json={**body, "expected_count": 2})
    assert r.status_code == 200 and r.json()["resolved"] == 2
    assert client.get("/incidents/summary").json()["open"] == 0


def test_detail_has_timeline_and_transitions(env):
    client, Session = env
    _make(Session, "fire", "urgent", n=2)
    inc_id = client.get("/incidents").json()["items"][0]["id"]
    client.post(f"/incidents/{inc_id}/acknowledge", json={})
    d = client.get(f"/incidents/{inc_id}").json()
    assert len(d["timeline"]) == 2
    assert [t["to_state"] for t in d["transitions"]] == ["open", "acknowledged"]


def test_export_uses_the_same_view(env):
    client, Session = env
    _make(Session, "fire", "urgent")
    _make(Session, "scene_review", "low", decision=policy.REVIEW)
    r = client.get("/incidents/export.xlsx", params={"view": "open"})
    assert r.status_code == 200
    import io

    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws.max_row == 2      # header + the one open incident


def test_legacy_rows_are_hidden(env):
    client, Session = env
    with Session() as db:
        db.add(Incident(incident_key="alert:1", camera_id=1, detection_type="fire",
                        queue="legacy", status="open"))
        db.commit()
    assert client.get("/incidents", params={"view": "all"}).json()["total"] == 0
