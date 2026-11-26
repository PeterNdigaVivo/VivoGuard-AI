"""Server-side incident deduplication, actions and count consistency."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import Alert, Camera, DetectionEvent, Store, User, Zone
from app.models.incident import Incident, IncidentMember, IncidentTransition
from app.services import alert_policy as policy
from app.services import incidents as svc

T0 = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    # VG_TEST_PG_URL runs the same tests against a real, empty Postgres
    # database (row locks and the partial unique index behave for real).
    pg_url = os.environ.get("VG_TEST_PG_URL")
    if pg_url:
        engine = create_engine(pg_url)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite:///:memory:")

        @sa_event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    store = Store(name="Junction", country="Kenya", timezone="Africa/Nairobi",
                  business_hours_json={"mon": [["09:30", "20:00"]]})
    s.add(store); s.flush()
    for i in (1, 2):
        s.add(Camera(name=f"Cam {i}", brand="generic", connection_type="lan_rtsp",
                     host="127.0.0.1", store_id=store.id))
    s.add(User(email="op@example.com", password_hash="x", role="operator"))
    s.flush()
    s.add(Zone(camera_id=1, name="Stockroom", polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
               detection_types_json=["restricted"]))
    s.commit()
    yield s
    s.close()


def _detect(db, *, camera_id=1, dtype="intrusion", zone_id=1, extra=None,
            decision=None, at=T0):
    extra = extra if extra is not None else {
        "time_context": "after_hours", "after_hours": True, "rule": "after_hours",
        "priority": "high"}
    ev = DetectionEvent(camera_id=camera_id, zone_id=zone_id, detection_type=dtype,
                        confidence=0.9, bbox_json=[0, 0, 1, 1], extra=extra,
                        timestamp=at)
    db.add(ev); db.flush()
    alert = Alert(event_id=ev.id, status="new")
    db.add(alert); db.flush()
    decision = decision or policy.decide(svc.build_policy_input(
        db, detection_type=dtype, confidence=0.9, extra=extra, zone_id=zone_id,
        store_id=1, frames_seen=6))
    return svc.attach_detection(db, alert, ev, decision, store_id=1, now=at), alert


def test_repeated_evidence_appends_to_one_incident(db):
    first, a1 = _detect(db)
    second, a2 = _detect(db, at=T0 + timedelta(seconds=40))
    third, a3 = _detect(db, at=T0 + timedelta(seconds=80))
    db.commit()

    assert first.outcome == "opened" and first.notify
    assert second.outcome == third.outcome == "appended"
    assert not second.notify
    assert db.query(Incident).count() == 1
    inc = db.query(Incident).one()
    assert inc.event_count == 3
    assert inc.head_alert_id == a1.id
    assert {a.incident_id for a in (a1, a2, a3)} == {inc.id}
    assert db.query(IncidentMember).count() == 3     # timeline kept
    assert db.query(Alert).count() == 3              # audit rows kept
    assert a2.notification_suppressed and a3.notification_suppressed
    assert not a1.notification_suppressed


def test_dedup_key_separates_camera_zone_detector_and_rule(db):
    _detect(db)
    _detect(db, camera_id=2, zone_id=None)
    _detect(db, extra={"time_context": "after_hours", "rule": "other_rule"})
    _detect(db, dtype="fire", zone_id=None, extra={"priority": "high"})
    db.commit()
    assert db.query(Incident).count() == 4


def test_gap_longer_than_window_renotifies_same_incident(db, monkeypatch):
    monkeypatch.setattr(settings, "incident_dedup_window_seconds", 600)
    _detect(db)
    later, _ = _detect(db, at=T0 + timedelta(minutes=30))
    db.commit()
    assert later.outcome == "renotified" and later.notify
    inc = db.query(Incident).one()
    assert inc.notify_count == 2 and inc.event_count == 2


def test_database_refuses_a_second_open_incident_for_one_key(db):
    _detect(db)
    db.commit()
    key = db.query(Incident.dedup_key).scalar()
    db.add(Incident(incident_key="dup", camera_id=1, store_id=1,
                    detection_type="intrusion", dedup_key=key, status="open"))
    with pytest.raises(Exception):
        db.flush()
    db.rollback()


def test_one_action_closes_the_whole_incident(db):
    _detect(db)
    _detect(db, at=T0 + timedelta(seconds=30))
    db.commit()
    inc = db.query(Incident).one()
    svc.apply_action(db, inc, "resolve", actor_user_id=1, reason="handled")
    db.commit()
    assert inc.status == "resolved" and inc.closed_by == 1
    assert {a.status for a in db.query(Alert).all()} == {"resolved"}
    assert db.query(IncidentTransition).filter_by(to_state="resolved").count() == 1
    with pytest.raises(svc.IncidentActionError):
        svc.apply_action(db, inc, "dismiss", actor_user_id=1)


def test_stale_version_is_rejected(db):
    _detect(db)
    db.commit()
    inc = db.query(Incident).one()
    with pytest.raises(svc.IncidentActionError):
        svc.apply_action(db, inc, "acknowledge", actor_user_id=1,
                         expected_version=inc.version - 1)


def test_evidence_after_dismissal_does_not_reopen_noise(db):
    noisy = policy.PolicyDecision(policy.INCIDENT, "detector_rule_matched",
                                  policy.ATTENTION)
    _detect(db, dtype="loitering", decision=noisy, extra={"rule": "loiter"})
    db.commit()
    svc.apply_action(db, db.query(Incident).one(), "dismiss",
                     actor_user_id=1, reason="normal_activity")
    db.commit()
    res, alert = _detect(db, dtype="loitering", decision=noisy,
                         extra={"rule": "loiter"}, at=T0 + timedelta(minutes=2))
    assert res.outcome == "after_close" and not res.notify
    assert alert.status == "dismissed"
    assert db.query(Incident).count() == 1


def test_urgent_evidence_after_resolve_opens_new_incident(db):
    _detect(db)
    db.commit()
    svc.apply_action(db, db.query(Incident).one(), "resolve", actor_user_id=1)
    db.commit()
    res, _ = _detect(db, at=T0 + timedelta(minutes=2))
    assert res.outcome == "opened"
    assert db.query(Incident).count() == 2


def test_review_item_is_promoted_when_real_evidence_arrives(db):
    review = policy.PolicyDecision(policy.REVIEW, "expected_early_staff", policy.LOW)
    first, a1 = _detect(db, decision=review)
    assert a1.review_only and a1.notification_suppressed
    assert db.query(Incident).one().queue == "review"
    real = policy.PolicyDecision(policy.INCIDENT, "confirmed_after_hours_intrusion",
                                 policy.URGENT)
    second, _ = _detect(db, decision=real, at=T0 + timedelta(seconds=20))
    inc = db.query(Incident).one()
    assert inc.queue == "incident" and inc.priority == "urgent"
    assert second.notify


def test_first_valid_entry_closes_store_not_opened(db):
    not_opened = policy.PolicyDecision(policy.INCIDENT, "store_not_opened_by_schedule",
                                       policy.ATTENTION)
    _detect(db, dtype="shop_open_close", zone_id=None, decision=not_opened,
            extra={"rule": "shop_not_opened"})
    db.commit()
    closed = svc.close_store_not_opened(db, store_id=1, evidence_event_id=99)
    db.commit()
    inc = db.query(Incident).one()
    assert closed == 1 and inc.status == "resolved"
    assert inc.close_reason == "first_valid_entry"


def test_counts_tabs_and_bulk_use_the_same_query(db):
    urgent = policy.PolicyDecision(policy.INCIDENT, "r", policy.URGENT)
    attention = policy.PolicyDecision(policy.INCIDENT, "r", policy.ATTENTION)
    review = policy.PolicyDecision(policy.REVIEW, "r", policy.LOW)
    _detect(db, dtype="fire", zone_id=None, decision=urgent)
    _detect(db, dtype="loitering", zone_id=None, decision=attention, extra={"rule": "a"})
    _detect(db, dtype="crowd", zone_id=None, decision=attention, extra={"rule": "b"})
    _detect(db, dtype="scene_review", zone_id=None, decision=review, extra={})
    db.commit()
    svc.apply_action(db, db.query(Incident).filter_by(detection_type="crowd").one(),
                     "resolve", actor_user_id=1)
    db.commit()

    summary = svc.summarize(db)
    for view in svc.VIEWS:
        listed = svc.incident_query(db, view=view).count()
        expected = {"open": summary["open"], "attention": summary["attention"],
                    "review": summary["review"], "resolved": summary["resolved"],
                    "all": summary["all"]}[view]
        assert listed == expected, view
    assert summary == {**summary, "urgent": 1, "attention": 1, "review": 1,
                       "resolved": 1, "open": 2, "all": 4}

    with pytest.raises(svc.IncidentActionError):
        svc.bulk_resolve(db, view="open", expected_count=5, actor_user_id=1)
    assert svc.summarize(db)["open"] == 2   # nothing changed on mismatch
    ids = svc.bulk_resolve(db, view="open", expected_count=2, actor_user_id=1)
    db.commit()
    assert len(ids) == 2
    after = svc.summarize(db)
    assert after["open"] == 0 and after["review"] == 1 and after["resolved"] == 3


def test_date_range_keeps_incidents_still_active_today(db):
    _detect(db, at=T0 - timedelta(days=1))
    _detect(db, at=T0 + timedelta(minutes=1))   # same key, beyond window
    db.commit()
    today = svc.incident_query(db, view="open", since=T0 - timedelta(hours=1),
                               until=T0 + timedelta(hours=23)).count()
    assert today == 1


def test_lifecycle_gate_needs_flag_and_schema(db, monkeypatch):
    svc._reset_schema_cache()
    monkeypatch.setattr(settings, "incident_lifecycle_enabled", False)
    assert svc.lifecycle_ready(db) is False
    monkeypatch.setattr(settings, "incident_lifecycle_enabled", True)
    assert svc.lifecycle_ready(db) is True
    svc._reset_schema_cache()


def test_backfill_folds_open_legacy_alerts(db):
    for i in range(3):
        ev = DetectionEvent(camera_id=1, zone_id=None, detection_type="fire",
                            confidence=0.9, bbox_json=[0, 0, 1, 1],
                            extra={"priority": "high"},
                            timestamp=T0 + timedelta(seconds=10 * i))
        db.add(ev); db.flush()
        db.add(Alert(event_id=ev.id, status="new",
                     created_at=T0 + timedelta(seconds=10 * i)))
    db.commit()
    stats = svc.backfill_open_alerts(db, since=T0 - timedelta(hours=1))
    db.commit()
    assert stats == {"seen": 3, "opened": 1, "appended": 2, "skipped": 0}
    assert db.query(Incident).one().event_count == 3


@pytest.mark.skipif(not os.environ.get("VG_TEST_PG_URL"),
                    reason="needs a real Postgres (VG_TEST_PG_URL)")
def test_two_workers_racing_open_exactly_one_incident(db):
    """Worker A opens (uncommitted); worker B's insert waits on the unique
    index, loses when A commits, and appends to A's incident instead."""
    import threading

    engine = db.get_bind()
    Session = sessionmaker(bind=engine)
    a, b = Session(), Session()
    res_a, _ = _detect(a)                      # A holds the open row
    out = {}

    def worker_b():
        try:
            out["b"] = _detect(b, at=T0 + timedelta(seconds=1))[0].outcome
            b.commit()
        except Exception as exc:  # pragma: no cover - surfaced below
            out["error"] = exc

    t = threading.Thread(target=worker_b)
    t.start()
    t.join(timeout=1.5)                        # B is blocked on A's insert
    a.commit()
    t.join(timeout=20)
    assert "error" not in out, out.get("error")
    assert res_a.outcome == "opened" and out["b"] == "appended"
    check = Session()
    assert check.query(Incident).count() == 1
    assert check.query(Incident).one().event_count == 2
    for s in (a, b, check):
        s.close()
