from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.alerts import alert_snapshot, alert_snapshot_at
from app.config import settings
from app.models import Alert, DetectionEvent


class _FakeDb:
    def __init__(self, alert, event):
        self.alert = alert
        self.event = event

    def get(self, model, _row_id):
        if model is Alert:
            return self.alert
        if model is DetectionEvent:
            return self.event
        return None


def test_missing_incident_snapshot_never_falls_back_to_live_frame() -> None:
    db = _FakeDb(
        SimpleNamespace(event_id=22),
        SimpleNamespace(thumbnail_path=None, camera_id=3),
    )

    with pytest.raises(HTTPException) as exc:
        alert_snapshot(11, db=db, _u=None)

    assert exc.value.status_code == 404
    assert exc.value.detail == "incident snapshot unavailable"


@pytest.mark.parametrize(("indexed", "relative_path", "valid"), [
    (False, "recordings/snapshots/2026-11-27/7.jpg", True),
    (True, "recordings/checkout_snaps/alerts/11/7.jpg", True),
    (True, "recordings/alert_snaps/8/11/7.jpg", True),
    (True, "recordings/after_hours_snaps/8/11/7.jpg", True),
    (False, "unrelated/7.jpg", False),
    (False, "recordings/snapshots/notes.txt", False),
    (False, "recordings/snapshots/empty.jpg", False),
    (True, "unrelated/7.jpg", False),
    (True, "recordings/checkout_snaps/pending/11/7.jpg", False),
    (True, "recordings/checkout_snaps/alerts/12/7.jpg", False),
    (True, "recordings/alert_snaps/8/12/7.jpg", False),
    (True, "recordings/after_hours_snaps/8/12/7.jpg", False),
])
def test_snapshot_paths_are_limited_to_the_expected_evidence(
    tmp_path, monkeypatch, indexed, relative_path, valid,
):
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path / "recordings"))
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"" if path.name == "empty.jpg" else b"synthetic stored evidence")
    db = _FakeDb(
        SimpleNamespace(event_id=22, snapshot_paths=[str(path)]),
        SimpleNamespace(thumbnail_path=str(path)),
    )

    read = alert_snapshot_at if indexed else alert_snapshot
    args = (11, 0) if indexed else (11,)
    if valid:
        response = read(*args, db=db, _u=None)
        assert response.path == str(path.resolve())
        assert response.media_type == "image/jpeg"
    else:
        with pytest.raises(HTTPException) as exc:
            read(*args, db=db, _u=None)
        assert exc.value.status_code == 404


def test_filmstrip_symlink_cannot_cross_alert_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    source = tmp_path / "alert_snaps" / "8" / "12" / "7.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic evidence for another alert")
    alias = tmp_path / "alert_snaps" / "8" / "11" / "7.jpg"
    alias.parent.mkdir(parents=True)
    try:
        alias.symlink_to(source)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows denied symbolic-link creation")
        raise
    db = _FakeDb(SimpleNamespace(event_id=22, snapshot_paths=[str(alias)]), None)

    with pytest.raises(HTTPException) as exc:
        alert_snapshot_at(11, 0, db=db, _u=None)
    assert exc.value.status_code == 404
