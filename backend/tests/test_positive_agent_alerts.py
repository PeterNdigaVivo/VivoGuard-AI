from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.alerts import _to_alert_out
from app.database import Base
from app.models import AgentReport, Alert, Camera, DetectionEvent
from app.services.alert_quality import quality_scorecards
from app.services.positive_alerts import POSITIVE_TYPE, emit_agent_recovery


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _camera(db):
    camera = Camera(
        name="Evidence anchor", site="Fleet", brand="generic",
        connection_type="lan_rtsp", host="127.0.0.1", status="online",
    )
    db.add(camera)
    db.flush()
    return camera


def _reports(db, previous_status="warning", current_status="ok"):
    previous = AgentReport(agent_name="streamer", status=previous_status)
    current = AgentReport(agent_name="streamer", status=current_status)
    db.add_all([previous, current])
    db.flush()
    return previous, current


def test_recovery_creates_silent_training_ineligible_positive(db):
    camera = _camera(db)
    previous, current = _reports(db)

    alert = emit_agent_recovery(db, previous, current)
    event = db.get(DetectionEvent, alert.event_id)

    assert alert.status == "resolved"
    assert alert.notification_suppressed is True
    assert alert.training_eligible is False
    assert alert.feedback_used_for_training is False
    assert event.detection_type == POSITIVE_TYPE
    assert event.camera_id == camera.id
    assert event.extra["positive_label"] == "POSITIVE – AUTOMATED"
    assert event.extra["evidence"] == {
        "previous_report_id": previous.id,
        "current_report_id": current.id,
        "previous_status": "warning",
        "current_status": "ok",
    }


@pytest.mark.parametrize("previous,current", [
    ("ok", "ok"), ("warning", "warning"), ("critical", "warning"),
])
def test_non_recovery_does_not_emit(db, previous, current):
    _camera(db)
    old, new = _reports(db, previous, current)
    assert emit_agent_recovery(db, old, new) is None
    assert db.query(Alert).count() == 0


def test_recovery_is_deduplicated_during_flapping_window(db):
    _camera(db)
    old, new = _reports(db)
    assert emit_agent_recovery(db, old, new) is not None
    old2, new2 = _reports(db, "critical", "ok")
    assert emit_agent_recovery(db, old2, new2) is None
    assert db.query(Alert).count() == 1


def test_positive_presentation_is_green_fleet_scoped_and_excluded_from_quality(db):
    camera = _camera(db)
    old, new = _reports(db)
    alert = emit_agent_recovery(db, old, new)
    event = db.get(DetectionEvent, alert.event_id)

    item = _to_alert_out(alert, event, camera, None, None)
    assert item.scope == "fleet"
    assert item.camera_id is None
    assert item.severity_label == "POSITIVE – AUTOMATED"
    assert item.severity_4_color == "#16a34a"
    assert item.severity_4_emoji == "✅"
    assert item.plain_title == "Agent Recovered — Streamer"
    assert quality_scorecards(db) == []


def test_expired_positive_does_not_block_later_recovery(db):
    _camera(db)
    old, new = _reports(db)
    first = emit_agent_recovery(db, old, new)
    event = db.get(DetectionEvent, first.event_id)
    event.timestamp = datetime.now(timezone.utc) - timedelta(days=8)
    old2, new2 = _reports(db, "warning", "ok")
    assert emit_agent_recovery(db, old2, new2) is not None
