"""Odoo sale-confirmed webhook: HMAC gate + visit correlation."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.integrations import router
from app.config import settings
from app.database import Base, get_db
from app.models import (Camera, SalesAICorrelation, Store, Zone,
                        ZoneVisit)

TABLES = [Store.__table__, Camera.__table__, Zone.__table__,
          ZoneVisit.__table__, SalesAICorrelation.__table__]

SECRET = "test-webhook-secret"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "odoo_webhook_secret", SECRET)
    # TestClient drives the async endpoint from another thread;
    # a shared StaticPool connection keeps the one in-memory DB
    # visible to both threads.
    eng = create_engine("sqlite://",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=TABLES)
    maker = sessionmaker(bind=eng)
    session = maker()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    yield TestClient(app), session
    session.close()


def _signed_post(client, payload: dict, *, secret: str = SECRET,
                 ts: float | None = None):
    body = json.dumps(payload).encode()
    ts = ts if ts is not None else time.time()
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return client.post("/integrations/odoo/sale-confirmed",
                       content=body,
                       headers={"X-Odoo-Timestamp": str(ts),
                                "X-Odoo-Signature": sig,
                                "Content-Type": "application/json"})


def _seed(session):
    store = Store(name="Vivo Junction", code="NRB-JCT", country="Kenya")
    session.add(store); session.flush()
    cam = Camera(name="Ch8", store_id=store.id, brand="dahua",
                 connection_type="rtsp", host="10.0.0.8")
    session.add(cam); session.flush()
    zone = Zone(camera_id=cam.id, name="Counter",
                polygon_coords_json=[[0, 0], [1, 0], [1, 1], [0, 1]],
                detection_types_json=["counter"])
    session.add(zone); session.commit()
    return store, cam, zone


def test_validated_when_visit_overlaps_sale(client):
    c, session = client
    store, cam, zone = _seed(session)
    sale_ts = datetime.now(timezone.utc)
    visit = ZoneVisit(store_id=store.id, camera_id=cam.id,
                      zone_id=zone.id,
                      entered_at=sale_ts - timedelta(minutes=3),
                      exited_at=sale_ts - timedelta(minutes=1),
                      duration_seconds=120)
    session.add(visit); session.commit()

    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5001,
                         "timestamp": sale_ts.isoformat(),
                         "amount_total": 4500.0})
    assert r.status_code == 200
    assert r.json() == {"status": "validated", "visit_id": visit.id}
    audit = session.query(SalesAICorrelation).one()
    assert audit.status == "validated"
    assert audit.zone_visit_id == visit.id


def test_open_visit_counts_as_presence(client):
    c, session = client
    store, cam, zone = _seed(session)
    sale_ts = datetime.now(timezone.utc)
    session.add(ZoneVisit(store_id=store.id, camera_id=cam.id,
                          zone_id=zone.id,
                          entered_at=sale_ts - timedelta(seconds=30)))
    session.commit()
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5002,
                         "timestamp": sale_ts.isoformat(),
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json()["status"] == "validated"


def test_unmatched_logged_when_no_presence(client):
    c, session = client
    _seed(session)
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5003,
                         "timestamp":
                             datetime.now(timezone.utc).isoformat(),
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json() == {"status": "unmatched",
                        "reason": "no_customer_presence"}
    audit = session.query(SalesAICorrelation).one()
    assert audit.status == "unmatched"
    assert audit.reason == "no_customer_presence"


def test_visit_outside_window_does_not_match(client):
    c, session = client
    store, cam, zone = _seed(session)
    sale_ts = datetime.now(timezone.utc)
    # Visit ended 30 minutes before the sale: outside the 10-min lookback.
    session.add(ZoneVisit(store_id=store.id, camera_id=cam.id,
                          zone_id=zone.id,
                          entered_at=sale_ts - timedelta(minutes=40),
                          exited_at=sale_ts - timedelta(minutes=30),
                          duration_seconds=600))
    session.commit()
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5004,
                         "timestamp": sale_ts.isoformat(),
                         "amount_total": 100.0})
    assert r.json()["status"] == "unmatched"


def test_hmac_gate(client, monkeypatch):
    c, session = client
    _seed(session)
    payload = {"store_code": "NRB-JCT", "order_id": 5005,
               "timestamp": datetime.now(timezone.utc).isoformat(),
               "amount_total": 1.0}
    # Wrong secret -> 401.
    assert _signed_post(c, payload, secret="wrong").status_code == 401
    # Stale timestamp -> 400.
    assert _signed_post(c, payload,
                        ts=time.time() - 3600).status_code == 400
    # Secret unset -> 503, fail closed.
    monkeypatch.setattr(settings, "odoo_webhook_secret", "")
    assert _signed_post(c, payload).status_code == 503


def test_unknown_store_audited_and_404(client):
    c, session = client
    _seed(session)
    r = _signed_post(c, {"store_code": "NOPE-1", "order_id": 5006,
                         "timestamp":
                             datetime.now(timezone.utc).isoformat(),
                         "amount_total": 1.0})
    assert r.status_code == 404
    audit = session.query(SalesAICorrelation).one()
    assert audit.reason == "unknown_store_code"
