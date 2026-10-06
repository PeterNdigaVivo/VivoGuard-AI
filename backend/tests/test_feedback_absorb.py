"""True/False verdicts reply fast; training samples are made in the background.

Covers record_verdict (saves + enqueues, never copies files in-request),
the feedback.absorb_verdict task (skips, retries with logging, never
touches the verdict) and the feedback.absorb_pending safety-net sweep.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.feedback_absorb as fa
import app.training.feedback_loop as fl
from app.database import Base
from app.models import Alert, AlertReviewDecision, Camera, DetectionEvent, User
from app.services.alert_feedback import record_verdict


@pytest.fixture()
def Session():
    # One shared in-memory database across sessions (StaticPool), so the
    # "worker" session sees what the "request" session committed.
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, status="new", eligible=True, used=False, ack_minutes_ago=None,
          resolved=False, decision=None):
    cam = db.query(Camera).first()
    if cam is None:
        cam = Camera(name="c", site="s", brand="dahua", connection_type="nvr_dahua",
                     host="127.0.0.1")
        db.add(cam)
        db.flush()
    user = db.query(User).first()
    if user is None:
        user = User(email="op@example.test", password_hash="x", role="operator")
        db.add(user)
        db.flush()
    now = datetime.now(timezone.utc)
    ev = DetectionEvent(camera_id=cam.id, detection_type="person", confidence=.9,
                        bbox_json=[0, 0, 1, 1], timestamp=now)
    db.add(ev)
    db.flush()
    a = Alert(event_id=ev.id, status=status, created_at=now,
              training_eligible=eligible, feedback_used_for_training=used,
              acknowledged_at=(now - timedelta(minutes=ack_minutes_ago)
                               if ack_minutes_ago is not None else None),
              resolved_at=now if resolved else None)
    db.add(a)
    db.flush()
    if decision:
        db.add(AlertReviewDecision(alert_id=a.id, reviewer_id=user.id, verdict=decision))
    db.commit()
    return a.id, user


@pytest.fixture()
def no_inline_absorb(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("training copy must not run inside the request")
    monkeypatch.setattr(fl, "absorb_confirmed", boom)
    monkeypatch.setattr(fl, "absorb_dismissed", boom)


# ── record_verdict (the request) ────────────────────────────────────

@pytest.mark.parametrize("verdict, status", [("confirm", "confirmed"), ("dismiss", "dismissed")])
def test_verdict_is_saved_then_training_is_queued(Session, monkeypatch, no_inline_absorb,
                                                  verdict, status):
    with Session() as db:
        alert_id, user = _seed(db)
        queued = []

        def capture(args, **_k):
            # The verdict must already be committed when the task is queued.
            with Session() as other:
                queued.append((args, other.get(Alert, args[0]).status))
        monkeypatch.setattr(fa.absorb_verdict, "apply_async", capture)

        out = record_verdict(db, alert_id, verdict, user)

    assert out.status == status
    assert queued == [([alert_id, verdict], status)]


def test_queue_failure_keeps_the_verdict_and_logs(Session, monkeypatch, no_inline_absorb, caplog):
    def down(*_a, **_k):
        raise ConnectionError("redis down")
    monkeypatch.setattr(fa.absorb_verdict, "apply_async", down)
    with Session() as db:
        alert_id, user = _seed(db)
        with caplog.at_level(logging.ERROR):
            out = record_verdict(db, alert_id, "dismiss", user)
    assert out.status == "dismissed"
    with Session() as db:
        assert db.get(Alert, alert_id).status == "dismissed"
    assert "could not queue the training sample" in caplog.text


def test_quality_controlled_alert_is_not_queued(Session, monkeypatch, no_inline_absorb):
    import app.services.alert_quality as aq
    monkeypatch.setattr(aq, "refresh_pair_control",
                        lambda *_a, **_k: type("S", (), {"mode": "quarantined"})())
    queued = []
    monkeypatch.setattr(fa.absorb_verdict, "apply_async", lambda *a, **k: queued.append(a))
    with Session() as db:
        alert_id, user = _seed(db)
        record_verdict(db, alert_id, "confirm", user)
        a = db.get(Alert, alert_id)
        assert a.training_eligible is False and a.review_only is True
    assert queued == []


def test_verdict_logs_its_timings(Session, monkeypatch, no_inline_absorb, caplog):
    monkeypatch.setattr(fa.absorb_verdict, "apply_async", lambda *a, **k: None)
    with Session() as db:
        alert_id, user = _seed(db)
        with caplog.at_level(logging.INFO, logger="app.services.alert_feedback"):
            record_verdict(db, alert_id, "confirm", user)
    assert f"verdict saved alert={alert_id} verdict=confirm" in caplog.text
    assert "total_ms=" in caplog.text


# ── absorb_verdict_now (the background work) ─────────────────────────

def test_absorbs_matching_verdict(Session, monkeypatch):
    calls = []
    monkeypatch.setattr(fl, "absorb_dismissed", lambda db, aid: calls.append(aid))
    with Session() as db:
        alert_id, _ = _seed(db, status="dismissed")
    assert fa.absorb_verdict_now(alert_id, "dismiss", Session) == "absorbed"
    assert calls == [alert_id]


@pytest.mark.parametrize("seed, verdict, expected", [
    (dict(status="new"), "dismiss", "verdict_changed"),          # e.g. undone
    (dict(status="confirmed"), "dismiss", "verdict_changed"),
    (dict(status="dismissed", used=True), "dismiss", "already_done"),
    (dict(status="dismissed", eligible=False), "dismiss", "not_eligible"),
])
def test_skips_without_touching_training(Session, monkeypatch, seed, verdict, expected):
    def boom(*_a, **_k):
        raise AssertionError("must not absorb")
    monkeypatch.setattr(fl, "absorb_dismissed", boom)
    with Session() as db:
        alert_id, _ = _seed(db, **seed)
    assert fa.absorb_verdict_now(alert_id, verdict, Session) == expected


def test_missing_alert(Session):
    assert fa.absorb_verdict_now(99999, "confirm", Session) == "missing"


def test_failure_never_changes_the_verdict(Session, monkeypatch):
    def broken(db, aid):
        db.get(Alert, aid).status = "new"     # a buggy helper must not leak
        raise OSError("disk full")
    monkeypatch.setattr(fl, "absorb_confirmed", broken)
    with Session() as db:
        alert_id, _ = _seed(db, status="confirmed")
    with pytest.raises(OSError):
        fa.absorb_verdict_now(alert_id, "confirm", Session)
    with Session() as db:
        assert db.get(Alert, alert_id).status == "confirmed"


# ── the Celery task: retries and logging ─────────────────────────────

class _Retry(Exception):
    pass


def _run_task(monkeypatch, retries_done):
    seen = {}

    def fake_retry(exc=None, countdown=None):
        seen["countdown"] = countdown
        raise _Retry()
    monkeypatch.setattr(fa.absorb_verdict, "retry", fake_retry)
    fa.absorb_verdict.push_request(retries=retries_done)
    try:
        return fa.absorb_verdict.run(7, "confirm"), seen
    finally:
        fa.absorb_verdict.pop_request()


def test_task_retries_with_backoff_and_logs(monkeypatch, caplog):
    def flaky(*_a, **_k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(fa, "absorb_verdict_now", flaky)
    with caplog.at_level(logging.WARNING, logger=fa.log.name):
        with pytest.raises(_Retry):
            _run_task(monkeypatch, retries_done=2)
    assert "attempt 3/6" in caplog.text and "db hiccup" in caplog.text


def test_task_gives_up_after_max_retries_and_logs(monkeypatch, caplog):
    def flaky(*_a, **_k):
        raise RuntimeError("still broken")
    monkeypatch.setattr(fa, "absorb_verdict_now", flaky)
    with caplog.at_level(logging.ERROR, logger=fa.log.name):
        result, seen = _run_task(monkeypatch, retries_done=fa.MAX_RETRIES)
    assert result is None and seen == {}
    assert "giving up" in caplog.text


def test_task_returns_outcome(monkeypatch):
    monkeypatch.setattr(fa, "absorb_verdict_now", lambda *_a, **_k: "absorbed")
    result, _ = _run_task(monkeypatch, retries_done=0)
    assert result == "absorbed"


def test_retry_delays_grow_and_cap():
    assert [fa.retry_delay_seconds(n) for n in range(6)] == [30, 60, 120, 240, 480, 900]
    assert fa.retry_delay_seconds(20) == 900


# ── the 15-minute safety-net sweep ───────────────────────────────────

def test_sweep_selects_only_real_unabsorbed_verdicts(Session):
    with Session() as db:
        want_d, _ = _seed(db, status="dismissed", ack_minutes_ago=60, decision="dismissed")
        want_c, _ = _seed(db, status="confirmed", ack_minutes_ago=90, decision="confirmed")
        _seed(db, status="confirmed", ack_minutes_ago=60, resolved=True,
              decision=None)                                           # /resolve, not True
        _seed(db, status="dismissed", ack_minutes_ago=60, used=True, decision="dismissed")
        _seed(db, status="dismissed", ack_minutes_ago=5, decision="dismissed")       # own task still running
        _seed(db, status="dismissed", ack_minutes_ago=60 * 24 * 8, decision="dismissed")  # too old
        _seed(db, status="dismissed", ack_minutes_ago=60, decision=None)             # no human decision
        _seed(db, status="dismissed", ack_minutes_ago=60, eligible=False, decision="dismissed")
        pending = fa.pending_verdicts(db)
    assert pending == [(want_d, "dismiss"), (want_c, "confirm")]      # newest first


def test_sweep_enqueues_each(monkeypatch):
    monkeypatch.setattr(fa, "pending_verdicts", lambda db: [(1, "confirm"), (2, "dismiss")])
    queued = []
    monkeypatch.setattr(fa.absorb_verdict, "apply_async", lambda args, **_k: queued.append(args))

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr("app.database.SessionLocal", lambda: _Ctx())
    assert fa.absorb_pending() == 2
    assert queued == [[1, "confirm"], [2, "dismiss"]]
