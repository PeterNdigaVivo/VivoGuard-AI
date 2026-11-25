"""Floor plans + camera placements (Heatmap Insights page)."""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.api import cameras as cameras_api
from app.api import stores as stores_api
from app.models import Camera, Store, User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    from app.config import settings
    monkeypatch.setattr(settings, "floor_plans_dir", str(tmp_path))

    app = FastAPI()
    app.include_router(stores_api.router)
    app.include_router(cameras_api.router)

    db = Session()
    store = Store(name="Vivo Test", country="Kenya")
    db.add(store); db.flush()
    cam = Camera(name="Till Cam", brand="dahua", connection_type="rtsp",
                 host="10.0.0.9", rtsp_port=554, http_port=80,
                 network_type="lan", store_id=store.id,
                 username="u", password_encrypted="x", status="online")
    db.add(cam); db.commit()
    store_id, cam_id = store.id, cam.id

    admin = User(email="t@vivo", password_hash="x", role="admin",
                 full_name="T")

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    # require_role() builds on get_current_user, so one override covers
    # both the plain-auth and role-gated endpoints.
    from app.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: admin

    c = TestClient(app)
    c.store_id = store_id
    c.cam_id = cam_id
    return c


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


def test_floor_plan_upload_and_serve(client):
    r = client.post(f"/stores/{client.store_id}/floor-plan",
                    files={"file": ("plan.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["floor_plan_path"]
    r2 = client.get(f"/stores/{client.store_id}/floor-plan")
    assert r2.status_code == 200
    assert r2.headers["content-type"] == "image/png"


def test_floor_plan_rejects_non_image(client):
    r = client.post(f"/stores/{client.store_id}/floor-plan",
                    files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 415


def test_floor_plan_404_before_upload(client):
    assert client.get(f"/stores/{client.store_id}/floor-plan").status_code == 404


def test_placement_roundtrip(client):
    body = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25, "rot": 90}
    r = client.put(f"/cameras/{client.cam_id}/floor-placement", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["floor_placement_json"] == body
    r2 = client.delete(f"/cameras/{client.cam_id}/floor-placement")
    assert r2.status_code == 200
    assert r2.json()["floor_placement_json"] is None


def test_placement_validation(client):
    bad = [
        {"x": 1.5, "y": 0, "w": 0.2, "h": 0.2, "rot": 0},
        {"x": 0, "y": 0, "w": 0.001, "h": 0.2, "rot": 0},
        {"x": 0, "y": 0, "w": 0.2, "h": 0.2, "rot": 45},
    ]
    for body in bad:
        r = client.put(f"/cameras/{client.cam_id}/floor-placement", json=body)
        assert r.status_code == 422, body
