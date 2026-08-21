"""Regression coverage for destructive camera deletion safeguards."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from app.api.cameras import delete_camera, restore_camera


class _FakeSession:
    def __init__(self, camera):
        self.camera = camera
        self.deleted = None
        self.committed = False

    def get(self, _model, camera_id):
        return self.camera if camera_id == self.camera.id else None

    def delete(self, camera):
        self.deleted = camera

    def commit(self):
        self.committed = True


def test_delete_camera_logs_actor_and_nonsecret_camera_identity(caplog) -> None:
    camera = SimpleNamespace(
        id=255,
        name="Capital Centre NVR - Channel 2",
        store_id=4,
        brand="dahua",
        host="camera.example.invalid",
        rtsp_port=554,
        channel_number=2,
        username="operator",
        password_encrypted="must-not-appear",
        rtsp_url_override="rtsp://secret:password@example.invalid/live",
        status="online",
        ai_enabled=True,
        is_deleted=False,
        deleted_at=None,
        deleted_by_user_id=None,
        deleted_by_email=None,
        deleted_previous_status=None,
        deleted_previous_ai_enabled=None,
        restored_at=None,
        restored_by_user_id=None,
        restored_by_email=None,
    )
    user = SimpleNamespace(id=7, email="admin@example.invalid")
    db = _FakeSession(camera)

    with caplog.at_level(logging.WARNING, logger="app.api.cameras"):
        result = delete_camera(255, db=db, _u=user)

    assert result == {"deleted": 255}
    assert db.deleted is None
    assert db.committed is True
    assert camera.is_deleted is True
    assert camera.status == "offline"
    assert camera.ai_enabled is False
    assert camera.deleted_previous_status == "online"
    assert camera.deleted_previous_ai_enabled is True
    assert camera.deleted_by_user_id == 7
    message = caplog.text
    assert "actor_user_id=7" in message
    assert "actor_email=admin@example.invalid" in message
    assert "camera_id=255" in message
    assert "Capital Centre NVR - Channel 2" in message
    assert "must-not-appear" not in message
    assert "secret:password" not in message


def test_restore_camera_reactivates_preserved_record(caplog) -> None:
    camera = SimpleNamespace(
        id=255,
        name="Capital Centre NVR - Channel 2",
        is_deleted=True,
        status="offline",
        ai_enabled=False,
        deleted_previous_status="pending",
        deleted_previous_ai_enabled=True,
        deleted_by_user_id=7,
        restored_at=None,
        restored_by_user_id=None,
        restored_by_email=None,
    )
    user = SimpleNamespace(id=8, email="recovery@example.invalid")
    db = _FakeSession(camera)

    with caplog.at_level(logging.WARNING, logger="app.api.cameras"):
        result = restore_camera(255, db=db, _u=user)

    assert result == {"restored": 255}
    assert db.committed is True
    assert camera.is_deleted is False
    assert camera.status == "pending"
    assert camera.ai_enabled is True
    assert camera.restored_by_user_id == 8
    assert "camera_restored" in caplog.text
