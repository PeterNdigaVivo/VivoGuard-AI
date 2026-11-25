"""AI floor-plan draft: endpoint behaviour with the model call mocked."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.api import stores as stores_api
from app.models import Camera, Store, User, Zone
from app.services import floor_plan_ai


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    from app.config import settings
    monkeypatch.setattr(settings, "floor_plans_dir", str(tmp_path))
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    app = FastAPI()
    app.include_router(stores_api.router)

    db = Session()
    store = Store(name="Vivo Junction", country="Kenya")
    db.add(store); db.flush()
    cams = []
    for name in ("Junction — Entrance", "Junction — Tills"):
        c = Camera(name=name, brand="dahua", connection_type="rtsp",
                   host="10.0.0.9", rtsp_port=554, http_port=80,
                   network_type="lan", store_id=store.id,
                   username="u", password_encrypted="x", status="online")
        db.add(c); db.flush(); cams.append(c)
    db.add(Zone(camera_id=cams[1].id, name="Till front",
                polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
                detection_types_json=["counter"]))
    db.commit()

    admin = User(email="t@vivo", password_hash="x", role="admin")

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    from app.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: admin

    # No Redis in tests: frame collection must degrade to text-only.
    monkeypatch.setattr(floor_plan_ai, "MAX_FRAMES", 0)

    return TestClient(app), db, store, cams


def _canned_reply(cams):
    return {
        "areas": [
            {"label": "TILLS", "kind": "area",
             "x": 0.7, "y": 0.4, "w": 0.25, "h": 0.3},
            {"label": "ENTRANCE", "kind": "entrance",
             "x": 0.4, "y": 0.95, "w": 0.2, "h": 0.05},
        ],
        "cameras": {
            str(cams[0].id): {"x": 0.3, "y": 0.6, "w": 0.4, "h": 0.4},
            str(cams[1].id): {"x": 0.65, "y": 0.35, "w": 0.3, "h": 0.4},
            "99999": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},  # unknown id: dropped
        },
    }


def test_generate_saves_plan_and_places_cameras(ctx, monkeypatch):
    client, db, store, cams = ctx
    seen = {}

    def fake_ask(user_text, images):
        seen["text"] = user_text
        return _canned_reply(cams)
    monkeypatch.setattr(floor_plan_ai, "_ask_model", fake_ask)

    r = client.post(f"/stores/{store.id}/floor-plan/generate")
    assert r.status_code == 200, r.text
    assert r.json()["floor_plan_path"]

    # The prompt carried the camera names and the counter zone tag.
    assert "Junction" in seen["text"] and "counter" in seen["text"]

    # Plan file is a real PNG.
    from PIL import Image
    img = Image.open(r.json()["floor_plan_path"])
    assert img.size == (floor_plan_ai.PLAN_W, floor_plan_ai.PLAN_H)

    # Both known cameras placed; the unknown id was dropped.
    db.expire_all()
    placed = {c.id: c.floor_placement_json for c in
              db.query(Camera).filter(Camera.store_id == store.id)}
    assert placed[cams[0].id]["rot"] == 0
    assert 0 <= placed[cams[1].id]["x"] <= 1
    # And it serves.
    assert client.get(f"/stores/{store.id}/floor-plan").status_code == 200


def test_generate_503_without_api_key(ctx, monkeypatch):
    client, db, store, cams = ctx
    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    r = client.post(f"/stores/{store.id}/floor-plan/generate")
    assert r.status_code == 503


def test_generate_502_on_garbage_reply(ctx, monkeypatch):
    client, db, store, cams = ctx
    monkeypatch.setattr(floor_plan_ai, "_ask_model",
                        lambda t, i: {"nope": True})
    r = client.post(f"/stores/{store.id}/floor-plan/generate")
    assert r.status_code == 502
    # Nothing was written on failure.
    db.expire_all()
    assert db.get(Store, store.id).floor_plan_path is None
