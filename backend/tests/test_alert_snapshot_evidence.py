from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.alerts import alert_snapshot
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
