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
from app.models import (Alert, Camera, DetectionEvent, OdooStoreMap,
                        SalesAICorrelation, Store, User, Zone, ZoneVisit)

TABLES = [Store.__table__, Camera.__table__, Zone.__table__,
          ZoneVisit.__table__, SalesAICorrelation.__table__,
          DetectionEvent.__table__, Alert.__table__, User.__table__,
          OdooStoreMap.__table__]

SECRET = "test-webhook-secret"

# 12:00 EAT on a Wednesday — inside the default 09:00-21:00 trading
# window, so the ghost-sale alert's open-hours guard passes.
OPEN_SALE_TS = "2026-11-25T12:00:00+03:00"
# 03:00 EAT — outside trading hours; the guard must suppress.
CLOSED_SALE_TS = "2026-11-25T03:00:00+03:00"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "odoo_webhook_secret", SECRET)
    # Quality control reaches tables/services this focused schema
    # doesn't carry; it has its own suite.
    monkeypatch.setattr("app.services.alert_quality.apply_quality_control",
                        lambda db, alert, event: None)
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


def test_unmatched_sale_logs_audit_and_raises_ghost_sale_alert(client):
    c, session = client
    store, cam, zone = _seed(session)
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5003,
                         "timestamp": OPEN_SALE_TS,
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json() == {"status": "unmatched",
                        "reason": "no_customer_presence",
                        "alert_created": True}
    audit = session.query(SalesAICorrelation).one()
    assert audit.status == "unmatched"
    assert audit.reason == "no_customer_presence"
    ev = session.query(DetectionEvent).one()
    assert ev.detection_type == "sales_attribution"
    assert ev.camera_id == cam.id and ev.zone_id == zone.id
    assert ev.extra["order_id"] == 5003
    assert session.query(Alert).count() == 1


def test_no_ghost_sale_alert_outside_trading_hours(client):
    c, session = client
    _seed(session)
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5004,
                         "timestamp": CLOSED_SALE_TS,
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json()["alert_created"] is False
    assert session.query(Alert).count() == 0


def test_no_ghost_sale_alert_without_counter_zone(client):
    c, session = client
    store = Store(name="Vivo Runda", code="NRB-RND", country="Kenya")
    session.add(store); session.flush()
    cam = Camera(name="Ch1", store_id=store.id, brand="dahua",
                 connection_type="rtsp", host="10.0.0.9")
    session.add(cam); session.commit()
    r = _signed_post(c, {"store_code": "NRB-RND", "order_id": 5005,
                         "timestamp": OPEN_SALE_TS,
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json()["alert_created"] is False
    assert session.query(Alert).count() == 0


def test_store_resolves_via_odoo_store_map(client):
    """Odoo sends ITS code/POS id, not VivoGuard's Store.code — the
    odoo_store_map fallback bridges the naming gap."""
    c, session = client
    store, cam, zone = _seed(session)
    session.add(OdooStoreMap(store_id=store.id, odoo_model="pos.config",
                             odoo_res_id=42, odoo_pos_config_id=42,
                             code="JUNCTION-POS", name="Junction POS"))
    session.commit()
    sale_ts = datetime.now(timezone.utc)
    session.add(ZoneVisit(store_id=store.id, camera_id=cam.id,
                          zone_id=zone.id,
                          entered_at=sale_ts - timedelta(minutes=2)))
    session.commit()
    # By the Odoo-side code...
    r = _signed_post(c, {"store_code": "JUNCTION-POS", "order_id": 6001,
                         "timestamp": sale_ts.isoformat(),
                         "amount_total": 100.0})
    assert r.status_code == 200 and r.json()["status"] == "validated"
    # ...and by the numeric pos_config id.
    r2 = _signed_post(c, {"store_code": "42", "order_id": 6002,
                          "timestamp": sale_ts.isoformat(),
                          "amount_total": 50.0})
    assert r2.status_code == 200 and r2.json()["status"] == "validated"


def test_ghost_sale_alerts_disabled_by_flag(client, monkeypatch):
    c, session = client
    _seed(session)
    monkeypatch.setattr(settings, "odoo_unmatched_sale_alerts", False)
    r = _signed_post(c, {"store_code": "NRB-JCT", "order_id": 5006,
                         "timestamp": OPEN_SALE_TS,
                         "amount_total": 100.0})
    assert r.status_code == 200
    assert r.json()["alert_created"] is False
    assert session.query(Alert).count() == 0


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
