import asyncio
from types import SimpleNamespace

from app.api import nvr as nvr_api
from app.api.nvr import (
    AddChannelsIn,
    QuickAddNvrIn,
    _channel_map,
    add_channels,
    quick_add_nvr,
)
from app.models import Camera, NVRDevice, Store
from app.schemas.camera import NVRChannelOut


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, store, cameras, nvr=None):
        self.store = store
        self.cameras = list(cameras)
        self.nvr = nvr
        self.added = []

    def get(self, model, row_id):
        if model is Store and row_id == self.store.id:
            return self.store
        if model is NVRDevice and self.nvr is not None and row_id == self.nvr.id:
            return self.nvr
        return None

    def query(self, model):
        assert model is Camera
        return _Query(self.cameras)

    def add(self, camera):
        camera.id = 1000 + len(self.added)
        self.added.append(camera)

    def commit(self):
        return None

    def refresh(self, _camera):
        return None


def _camera(camera_id: int, channel: int) -> SimpleNamespace:
    return SimpleNamespace(id=camera_id, channel_number=channel)


def test_channel_map_keeps_oldest_duplicate() -> None:
    oldest = _camera(3, 1)
    duplicate = _camera(8, 1)
    channel_two = _camera(4, 2)

    assert _channel_map([duplicate, oldest, channel_two]) == {
        1: oldest,
        2: channel_two,
    }


def test_quick_add_is_idempotent_for_existing_host_port_store_channel(
    monkeypatch,
) -> None:
    store = SimpleNamespace(id=64, name="Vivo Capital Centre",
                            default_rtsp_port=None)
    existing = SimpleNamespace(id=254, channel_number=1)
    db = _Session(store, [existing])
    monkeypatch.setattr(nvr_api, "encrypt", lambda _password: "encrypted")

    result = quick_add_nvr(
        QuickAddNvrIn(
            store_id=64,
            brand="dahua",
            host="camera.example.invalid",
            username="operator",
            password="secret",
            channel_count=4,
        ),
        db=db,
        _u=SimpleNamespace(),
    )

    assert len(result) == 4
    assert result[0] is existing
    assert [camera.channel_number for camera in db.added] == [2, 3, 4]


def test_add_channels_is_idempotent_for_existing_nvr_channel() -> None:
    store = SimpleNamespace(id=64)
    nvr = SimpleNamespace(
        id=9,
        name="Capital Centre NVR",
        brand="dahua",
        host="camera.example.invalid",
        rtsp_port=554,
        http_port=80,
        username="operator",
        password_encrypted="encrypted",
    )
    existing = SimpleNamespace(id=254, channel_number=1)
    db = _Session(store, [existing], nvr=nvr)
    channels = [
        NVRChannelOut(channel=1, name="Channel 1", rtsp_main="rtsp://one",
                      rtsp_sub="rtsp://one/sub"),
        NVRChannelOut(channel=2, name="Channel 2", rtsp_main="rtsp://two",
                      rtsp_sub="rtsp://two/sub"),
    ]

    result = asyncio.run(add_channels(
        9,
        AddChannelsIn(store_id=64, channels=channels),
        db=db,
        _u=SimpleNamespace(),
    ))

    assert len(result) == 2
    assert result[0] is existing
    assert [camera.channel_number for camera in db.added] == [2]
    assert db.added[0].rtsp_url_override is None
