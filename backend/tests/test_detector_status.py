"""Detector catalogue states come from configuration and reviews, not a label."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.detector_catalog import DETECTOR_CATALOG, router
from app.database import Base, get_db
from app.deps import get_current_user
from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, Camera, DetectionConfig,
    DetectionEvent, Store, User, Zone,
)


@pytest.fixture()
def env():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Store(name="Junction", country="Kenya",
                     business_hours_json={"mon": [["09:30", "20:00"]]}))
        db.add(Store(name="Runda", country="Kenya"))           # no hours
        db.flush()
        for i, store in ((1, 1), (2, 1), (3, 2), (4, 1)):
            db.add(Camera(name=f"C{i}", brand="generic", connection_type="lan_rtsp",
                          host="127.0.0.1", store_id=store, ai_enabled=True,
                          is_deleted=(i == 4)))
        db.add(User(email="op@vivo.test", password_hash="x", role="operator"))
        db.add(User(email="admin@vivo.test", password_hash="x", role="admin"))
        db.flush()
        # intrusion via a restricted zone on cams 1 and 3 (3's store: no hours)
        for cam in (1, 3):
            db.add(Zone(camera_id=cam, name="Stock", polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
                        detection_types_json=["restricted"]))
        # queue enabled by config but no queue zone drawn
        db.add(DetectionConfig(camera_id=2, detection_type="queue", enabled=True))
        # fall explicitly disabled
        db.add(DetectionConfig(camera_id=1, detection_type="fall", enabled=False))
        # crowd: 60 reviewed, 57 true -> validated
        db.add(DetectionConfig(camera_id=1, detection_type="crowd", enabled=True))
        for k in range(60):
            ev = DetectionEvent(camera_id=1, detection_type="crowd", confidence=.9,
                                bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc))
            db.add(ev); db.flush()
            a = Alert(event_id=ev.id, status="new"); db.add(a); db.flush()
            db.add(AlertReviewDecision(alert_id=a.id, reviewer_id=1,
                                       verdict="confirmed" if k < 57 else "dismissed"))
        # loitering configured but quarantined on one camera
        db.add(DetectionConfig(camera_id=2, detection_type="loitering", enabled=True))
        db.add(AlertQualityControl(camera_id=2, detection_type="loitering",
                                   mode="quarantined"))
        db.commit()

    def client(role):
        app = FastAPI()
        app.include_router(router)

        def _db():
            with Session() as s:
                yield s
        app.dependency_overrides[get_db] = _db
        with Session() as s:
            u = s.query(User).filter_by(role=role).one()
            s.expunge(u)
        app.dependency_overrides[get_current_user] = lambda: u
        return TestClient(app)
    return client


def test_states_reflect_real_configuration(env):
    cat = env("operator").get("/detectors/catalog").json()
    assert set(cat) == set(DETECTOR_CATALOG)
    assert all(v["state"] in ("available", "configured", "validated", "review_only",
                              "training_required", "disabled") for v in cat.values())
    assert "active" not in {v["state"] for v in cat.values()}

    assert cat["weapon"]["state"] == "training_required"
    assert cat["fall"]["state"] == "disabled"
    assert cat["tripwire"]["state"] == "available"
    assert cat["crowd"]["state"] == "validated"
    assert cat["crowd"]["precision_30d"] == 0.95
    assert cat["crowd"]["next_action"] == "None"
    assert cat["loitering"]["state"] == "review_only"
    assert cat["shelf_change"]["state"] == "available"   # not configured anywhere


def test_requirements_and_next_actions(env):
    cat = env("operator").get("/detectors/catalog").json()
    intr = cat["intrusion"]
    assert intr["configured_cameras"] == 2            # deleted camera 4 excluded
    assert intr["needs_schedule"] and intr["stores_missing_hours"] == 1
    assert intr["state"] == "configured"
    assert intr["next_action"] == "Set opening hours for 1 store(s)"
    q = cat["queue"]
    assert q["configured_cameras"] == 1 and q["cameras_missing_zone"] == 1
    assert q["next_action"] == "Draw the required zone on 1 camera(s)"
    assert q["zone_tags"] == ["queue"]
    # always-on analytics count every active camera
    assert cat["heatmap"]["configured_cameras"] == 3


def test_implementation_notes_hidden_from_operators(env):
    op = env("operator").get("/detectors/catalog").json()
    assert all("admin_note" not in v and "training_hint" not in v for v in op.values())
    admin = env("admin").get("/detectors/catalog").json()
    assert "admin_note" in admin["weapon"]
    assert all("pip" not in (v.get("admin_note") or "").lower() for v in admin.values())
