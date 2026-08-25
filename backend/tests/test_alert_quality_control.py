from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.api.alerts import _to_alert_out, alerts_summary, resolve_all
from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, AssuranceCase, Camera,
    DetectionEvent, RecordingClip, User,
)
from app.operations.assurance import create_alert_quality_cases
from app.services.alert_feedback import record_verdict
from app.tasks.alerting import _info_notification_allowed
from app.services.alert_quality import (
    _wilson_lower_bound, apply_quality_control, pair_metrics,
    quality_scorecards, refresh_pair_control,
    set_manual_mode,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _camera(db):
    cam = Camera(name="Junction Channel 1", site="Vivo Junction",
                 brand="dahua", connection_type="nvr_dahua",
                 host="127.0.0.1")
    db.add(cam)
    db.flush()
    return cam


def _alert(db, cam, status="new", *, clip=False, age_minutes=0):
    ev = DetectionEvent(camera_id=cam.id, detection_type="intrusion",
                        confidence=.9, bbox_json=[0, 0, 1, 1],
                        clip_path="/clip.mp4" if clip else None,
                        timestamp=datetime.now(timezone.utc)
                                  - timedelta(minutes=age_minutes))
    db.add(ev)
    db.flush()
    alert = Alert(event_id=ev.id, status=status,
                  created_at=ev.timestamp)
    db.add(alert)
    db.flush()
    return alert, ev


def test_false_rate_opens_quarantine_and_does_not_auto_recover(db):
    cam = _camera(db)
    for index in range(20):
        _alert(db, cam, "dismissed" if index < 10 else "confirmed",
               age_minutes=index)
    state = refresh_pair_control(db, cam.id, "intrusion")
    assert state.mode == "quarantined"
    assert state.last_sample_size == 20
    assert state.last_false_rate == pytest.approx(.5)

    # A later good sample does not silently re-enable notifications.
    _alert(db, cam, "confirmed")
    state = refresh_pair_control(db, cam.id, "intrusion")
    assert state.mode == "quarantined"


def test_quarantined_alert_retains_evidence_but_suppresses_escalation(db):
    cam = _camera(db)
    state = AlertQualityControl(camera_id=cam.id,
                                detection_type="intrusion",
                                mode="quarantined", reason="poor precision")
    db.add(state)
    alert, event = _alert(db, cam, clip=True)
    apply_quality_control(db, alert, event)

    assert alert.id is not None
    assert event.clip_path == "/clip.mp4"
    assert alert.review_only is True
    assert alert.notification_suppressed is True
    assert alert.training_eligible is False
    assert event.extra["quality_control"]["training_eligible"] is False

    item = _to_alert_out(alert, event, cam, None, None)
    assert item.review_only is True
    assert item.notification_suppressed is True
    assert item.quality_mode == "quarantined"
    assert item.quality_reason == "poor precision"
    assert item.clip_status == "pending"


def test_alert_without_video_surfaces_snapshot_timeline_fallback(db):
    cam = _camera(db)
    alert, event = _alert(db, cam, age_minutes=10)
    alert.snapshot_paths = ["/snapshots/one.jpg", "/snapshots/two.jpg"]
    db.flush()

    item = _to_alert_out(alert, event, cam, None, None)

    assert item.clip_url is None
    assert item.clip_status == "unavailable"
    assert item.snapshot_count == 2
    assert item.snapshot_paths == ["/snapshots/one.jpg", "/snapshots/two.jpg"]


def test_operational_summary_separates_quarantined_evidence(db):
    cam = _camera(db)
    operational, _ = _alert(db, cam)
    calibration, _ = _alert(db, cam)
    calibration.review_only = True
    calibration.notification_suppressed = True
    db.flush()

    summary = alerts_summary.__wrapped__(db=db, _u=None, store_id=None)

    assert summary["today_count"] == 2
    assert summary["operational_today_count"] == 1
    assert summary["calibration_today"] == 1
    assert summary["urgent"] == 1
    assert summary["critical_today"] == 1
    assert summary["unread_urgent"] == 1


def test_resolve_all_never_closes_quarantined_review_evidence(db):
    cam = _camera(db)
    operational, _ = _alert(db, cam)
    calibration, _ = _alert(db, cam)
    calibration.review_only = True
    calibration.notification_suppressed = True
    user = User(email="operator@example.test", password_hash="not-used",
                role="operator")
    db.add(user)
    db.commit()

    result = resolve_all(db=db, user=user, store_id=None,
                         since=None, until=None)

    db.refresh(operational)
    db.refresh(calibration)
    assert result == {"resolved": 1}
    assert operational.status == "confirmed"
    assert calibration.status == "new"


def test_release_is_evidence_gated_but_force_is_attributed(db):
    cam = _camera(db)
    state = AlertQualityControl(
        camera_id=cam.id, detection_type="intrusion", mode="quarantined",
        reviewed_count_at_quarantine=0, reason="poor precision")
    db.add(state)
    for _ in range(5):
        _alert(db, cam, "confirmed")
    with pytest.raises(ValueError, match="20 post-quarantine"):
        set_manual_mode(db, cam.id, "intrusion", "active",
                        reason="walkthrough looked good", actor="admin@vivo")
    released = set_manual_mode(
        db, cam.id, "intrusion", "active",
        reason="Emergency admin release after physical acceptance test",
        actor="admin@vivo", force=True)
    assert released.mode == "active"
    assert released.source == "manual"
    assert released.changed_by == "admin@vivo"


def test_scorecard_is_honest_about_precision_recall_and_agreement(db):
    cam = _camera(db)
    _alert(db, cam, "confirmed", clip=True)
    _alert(db, cam, "dismissed")
    _alert(db, cam, "new")
    cards = quality_scorecards(db)
    card = cards[0]
    assert card["true_alerts"] == 1
    assert card["false_alerts"] == 1
    assert card["unreviewed_alerts"] == 1
    assert card["reviewed_sample_size"] == 2
    assert card["precision"] == pytest.approx(.5)
    assert card["precision_lower_bound_95"] < card["precision"]
    assert card["target_99_evidence_met"] is False
    assert card["recall"] is None
    assert "missed events" in card["recall_limitation"]
    assert card["incident_clips_available"] == 1
    assert card["clip_eligible_alerts"] == 1
    assert card["clip_ineligible_alerts"] == 2
    assert card["clip_eligibility_unknown"] == 0
    assert card["clip_availability_rate"] == 1.0
    assert card["clip_availability_limitation"] is None
    assert card["reviewer_agreement"] is None


def test_scorecard_counts_legacy_extra_clip_path(db):
    cam = _camera(db)
    _alert_row, event = _alert(db, cam, "confirmed")
    event.extra = {"alert_clip_path": "/legacy/incident.mp4"}
    db.commit()

    card = quality_scorecards(db)[0]
    assert card["incident_clips_available"] == 1
    assert card["clip_eligible_alerts"] == 1
    assert card["clip_availability_rate"] == 1.0


def test_scorecard_excludes_alert_without_recording_from_clip_sla(db):
    cam = _camera(db)
    _alert(db, cam, "confirmed")

    card = quality_scorecards(db)[0]

    assert card["incident_clips_available"] == 0
    assert card["clip_eligible_alerts"] == 0
    assert card["clip_ineligible_alerts"] == 1
    assert card["clip_eligibility_unknown"] == 0
    assert card["clip_availability_rate"] is None


def test_scorecard_uses_retained_recording_as_legacy_clip_denominator(db):
    cam = _camera(db)
    _alert_row, event = _alert(db, cam, "confirmed")
    db.add(RecordingClip(
        camera_id=cam.id, store_id=cam.store_id,
        window_id="legacy-test", file_path=None,
        started_at=event.timestamp - timedelta(seconds=30),
        ended_at=event.timestamp + timedelta(seconds=30),
        status="deleted",
    ))
    db.commit()

    card = quality_scorecards(db)[0]

    assert card["incident_clips_available"] == 0
    assert card["clip_eligible_alerts"] == 1
    assert card["clip_ineligible_alerts"] == 0
    assert card["clip_availability_rate"] == 0.0


def test_threshold_crossing_verdict_is_not_sent_to_training(db, monkeypatch):
    cam = _camera(db)
    for index in range(19):
        _alert(db, cam, "dismissed" if index < 9 else "confirmed",
               age_minutes=index + 1)
    current, _ = _alert(db, cam)
    user = User(email="reviewer@vivo", password_hash="x", role="operator")
    db.add(user)
    db.commit()
    called = []
    monkeypatch.setattr("app.training.feedback_loop.mark_dismissed",
                        lambda *_args: called.append(True))

    record_verdict(db, current.id, "dismiss", user)
    db.refresh(current)
    assert current.review_only is True
    assert current.notification_suppressed is True
    assert current.training_eligible is False
    assert called == []
    decisions = db.query(AlertReviewDecision).filter_by(alert_id=current.id).all()
    assert [(row.reviewer_id, row.verdict) for row in decisions] == [
        (user.id, "dismissed")]


def test_scorecard_measures_latest_distinct_reviewer_disagreement(db):
    cam = _camera(db)
    alert, _ = _alert(db, cam, "confirmed", clip=True)
    first = User(email="first@vivo", password_hash="x", role="operator")
    second = User(email="second@vivo", password_hash="x", role="operator")
    db.add_all([first, second])
    db.flush()
    # History remains append-only. The first reviewer's latest position is
    # dismissed, which disagrees with the second reviewer's confirmation.
    db.add_all([
        AlertReviewDecision(alert_id=alert.id, reviewer_id=first.id,
                            verdict="confirmed"),
        AlertReviewDecision(alert_id=alert.id, reviewer_id=first.id,
                            verdict="dismissed"),
        AlertReviewDecision(alert_id=alert.id, reviewer_id=second.id,
                            verdict="confirmed"),
    ])
    db.commit()
    card = quality_scorecards(db)[0]
    assert card["multi_reviewer_alerts"] == 1
    assert card["reviewer_agreement"] == 0
    assert card["reviewer_agreement_count"] == 0
    assert card["reviewer_disagreement_count"] == 1
    assert db.query(AlertReviewDecision).count() == 3


def test_resolving_alert_does_not_erase_append_only_human_verdict(db):
    cam = _camera(db)
    alert, _event = _alert(db, cam, "resolved", clip=True)
    reviewer = User(email="reviewer@vivo", password_hash="x", role="operator")
    db.add(reviewer)
    db.flush()
    db.add(AlertReviewDecision(
        alert_id=alert.id, reviewer_id=reviewer.id, verdict="confirmed"))
    db.commit()

    card = quality_scorecards(db)[0]
    metrics = pair_metrics(db, cam.id, "intrusion")

    assert card["true_alerts"] == 1
    assert card["reviewed_sample_size"] == 1
    assert metrics["true_alerts"] == 1
    assert metrics["sample_size"] == 1


def test_99_percent_gate_requires_sample_size_and_confidence():
    assert _wilson_lower_bound(299, 300) < .99
    assert _wilson_lower_bound(300, 300) >= .99


def test_overdue_review_only_critical_alert_opens_accountable_case(db):
    cam = _camera(db)
    alert, event = _alert(db, cam, age_minutes=10)
    alert.review_only = True
    alert.training_eligible = False
    alert.notification_suppressed = True
    event.extra = {"quality_control": {"mode": "quarantined"}}
    db.commit()

    count = create_alert_quality_cases(db, now=datetime.now(timezone.utc))
    assert count == 1
    case = db.query(AssuranceCase).one()
    assert case.severity == "critical"
    assert "review SLA" in case.title
    assert case.evidence["accountable_owner"] == "Loss Prevention Operations"
    assert case.evidence["quality_mode"] == "quarantined"
    assert "quality_controlled_review_sla_breached" in case.evidence["issues"]
    create_alert_quality_cases(db, now=datetime.now(timezone.utc))
    assert db.query(AssuranceCase).count() == 1


def test_recorder_clip_clears_false_missing_evidence_case(db, tmp_path):
    cam = _camera(db)
    alert, event = _alert(db, cam, age_minutes=1)
    now = datetime.now(timezone.utc)

    assert create_alert_quality_cases(db, now=now) == 1
    case = db.query(AssuranceCase).one()
    assert case.evidence["issues"] == ["evidence_missing"]
    assert case.status == "open"

    clip = tmp_path / "incident.mp4"
    clip.write_bytes(b"playable evidence")
    event.extra = {"alert_clip_path": str(clip)}
    db.commit()

    assert create_alert_quality_cases(db, now=now) == 0
    db.flush()
    db.refresh(case)
    assert case.status == "resolved"
    assert case.resolved_at is not None
    assert alert.status == "new"


def test_direct_task_notification_obeys_persisted_quality_decision():
    class Event:
        extra = {"quality_control": {"notification_suppressed": True}}

    assert _info_notification_allowed(Event()) is False
