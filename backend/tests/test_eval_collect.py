"""Building the evaluation test set (app/eval/collect.py, raw frames, pruning)."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.eval.collect as collect
from app.database import Base
from app.eval.raw_frames import raw_path_for, save_raw_frame
from app.models import (
    Alert, AlertReviewDecision, Camera, DetectionEvent, EvalSample, User, Zone,
)

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)      # 12:00 Nairobi (busy)


@pytest.fixture()
def Session():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def world(Session):
    with Session() as db:
        cam = Camera(name="Counter", site="s", brand="dahua", connection_type="nvr_dahua",
                     host="127.0.0.1")
        db.add(cam)
        db.flush()
        user = User(email="op@example.test", password_hash="x", role="operator")
        db.add(user)
        db.flush()
        zone = Zone(camera_id=cam.id, name="Counter", polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
                    detection_types_json=["counter"])
        db.add(zone)
        db.commit()
        return {"cam": cam.id, "user": user.id, "zone": zone.id}


def _alert(Session, world, tmp_path, *, dtype="staff_present", rule="counter_unattended",
           verdicts=("dismissed",), raw=True, clip=False, age_days=0):
    with Session() as db:
        extra = {"rule": rule}
        if raw:
            p = tmp_path / f"raw_{len(os.listdir(tmp_path))}.jpg"
            p.write_bytes(b"jpeg-bytes")
            extra["raw_snapshot_path"] = str(p)
        if clip:
            c = tmp_path / f"clip_{len(os.listdir(tmp_path))}.mp4"
            c.write_bytes(b"mp4-bytes")
            extra["alert_clip_path"] = str(c)
        ts = NOW - timedelta(days=age_days)
        ev = DetectionEvent(camera_id=world["cam"], zone_id=world["zone"], detection_type=dtype,
                            confidence=.9, bbox_json=[.1, .1, .4, .9], timestamp=ts, extra=extra)
        db.add(ev)
        db.flush()
        a = Alert(event_id=ev.id, status="new", created_at=ts)
        db.add(a)
        db.flush()
        for i, v in enumerate(verdicts):
            db.add(AlertReviewDecision(alert_id=a.id, reviewer_id=world["user"], verdict=v,
                                       created_at=ts + timedelta(seconds=i + 1)))
        db.commit()
        return a.id


def _collect(Session, root, **kw):
    with Session() as db:
        return collect.collect_reviewed_alerts(
            db, root=root, target_per_category=kw.get("target", 300),
            clip_target=kw.get("clip_target", 100), now=NOW)


def test_collects_reviewed_alert_with_clean_frame(Session, world, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    root = tmp_path / "eval"
    # Latest decision wins: first True, then changed to False.
    aid = _alert(Session, world, src, verdicts=("confirmed", "dismissed"))
    stats = _collect(Session, root)
    assert stats["added"] == 1
    with Session() as db:
        s = db.query(EvalSample).one()
        assert (s.kind, s.alert_id, s.category, s.verdict, s.status) == (
            "alert", aid, "counter_unattended", "false", "labelled")
        assert s.zone_polygon == [[0, 0], [1, 0], [1, 1]]
        assert s.event_bbox == [.1, .1, .4, .9]
        assert open(s.frame_path, "rb").read() == b"jpeg-bytes"
        assert stat.S_IMODE(os.stat(s.frame_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root / "frames").st_mode) == 0o700
    assert _collect(Session, root)["added"] == 0                       # never twice


def test_skips_unreviewed_unevaluated_and_frameless(Session, world, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _alert(Session, world, src, verdicts=())                          # not reviewed
    _alert(Session, world, src, dtype="fire", rule=None)              # not an evaluated type
    _alert(Session, world, src, rule="other_rule")                    # staff_present, other rule
    _alert(Session, world, src, raw=False)                            # no clean frame, no clip
    _alert(Session, world, src, age_days=30)                          # outside the lookback
    stats = _collect(Session, tmp_path / "eval")
    assert stats["added"] == 0
    assert stats["no_clean_frame"] == 1


def test_clip_fallback_and_clip_sample(Session, world, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    seen = {}

    def fake_frame(path, second):
        seen["second"] = second
        return b"frame-from-clip"
    monkeypatch.setattr(collect, "frame_from_clip", fake_frame)
    _alert(Session, world, src, dtype="intrusion", rule=None, raw=False, clip=True,
           verdicts=("confirmed",))
    stats = _collect(Session, tmp_path / "eval")
    assert stats == {"added": 1, "clips_added": 1, "no_clean_frame": 0, "not_evaluated": 0}
    assert seen["second"] == collect.CLIP_EVENT_SECOND
    with Session() as db:
        alert_s = db.query(EvalSample).filter_by(kind="alert").one()
        clip_s = db.query(EvalSample).filter_by(kind="clip").one()
        assert alert_s.category == "person" and alert_s.verdict == "true"
        assert open(alert_s.frame_path, "rb").read() == b"frame-from-clip"
        assert clip_s.status == "pending" and os.path.isfile(clip_s.clip_path)


def test_target_per_category_is_respected(Session, world, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for _ in range(3):
        _alert(Session, world, src)
    assert _collect(Session, tmp_path / "eval", target=2)["added"] == 2
    assert _collect(Session, tmp_path / "eval", target=2)["added"] == 0


class _Redis:
    def __init__(self, data):
        self.data = data

    def get(self, key):
        return self.data.get(key)


class _Frames:
    def latest_jpeg(self, cid, prefer_overlay=True):
        assert prefer_overlay is False            # must be the clean frame
        return f"jpeg-{cid}".encode()


def test_people_frames_from_busy_cameras_only(Session, world, tmp_path):
    ts = NOW.timestamp()
    with Session() as db:
        quiet = Camera(name="q", site="s", brand="dahua", connection_type="nvr_dahua",
                       host="127.0.0.1")
        stale = Camera(name="t", site="s", brand="dahua", connection_type="nvr_dahua",
                       host="127.0.0.1")
        db.add_all([quiet, stale])
        db.commit()
        ids = (world["cam"], quiet.id, stale.id)
    redis_data = {
        f"vg:activity:{ids[0]}": json.dumps({"people": 3, "ts": ts - 10}),
        f"vg:activity:{ids[1]}": json.dumps({"people": 0, "ts": ts - 10}),
        f"vg:activity:{ids[2]}": json.dumps({"people": 5, "ts": ts - 3600}),
    }
    root = tmp_path / "eval"
    with Session() as db:
        added = collect.sample_people_frames(db, _Redis(redis_data), _Frames(), root=root,
                                             target=10, now=NOW)
        assert added == 1
        s = db.query(EvalSample).one()
        assert (s.kind, s.camera_id, s.status) == ("people", ids[0], "pending")
        assert open(s.frame_path, "rb").read() == f"jpeg-{ids[0]}".encode()
        # Cooldown: the same camera is not sampled again within hours.
        assert collect.sample_people_frames(db, _Redis(redis_data), _Frames(), root=root,
                                            target=10, now=NOW + timedelta(minutes=30)) == 0
        # Outside busy hours (03:00 Nairobi): nothing.
        assert collect.sample_people_frames(
            db, _Redis(redis_data), _Frames(), root=root, target=10,
            now=datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)) == 0


def test_prune_deletes_old_rows_and_files(Session, tmp_path):
    old_file = tmp_path / "old.jpg"
    old_file.write_bytes(b"x")
    new_file = tmp_path / "new.jpg"
    new_file.write_bytes(b"x")
    with Session() as db:
        db.add_all([
            EvalSample(kind="people", frame_path=str(old_file), status="labelled",
                       created_at=NOW - timedelta(days=400)),
            EvalSample(kind="people", frame_path=str(new_file), status="labelled",
                       created_at=NOW - timedelta(days=10)),
        ])
        db.commit()
        assert collect.prune_samples(db, retention_days=365, now=NOW) == 1
        assert db.query(EvalSample).count() == 1
    assert not old_file.exists() and new_file.exists()


# ── clean frame on the alert path ─────────────────────────────────────

def test_save_raw_frame_beside_snapshot(tmp_path):
    snap = tmp_path / "staff_present_7_123.jpg"
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    out = save_raw_frame(frame, str(snap))
    assert out == raw_path_for(str(snap)) == str(tmp_path / "staff_present_7_123_raw.jpg")
    assert os.path.isfile(out)
    assert save_raw_frame(None, str(snap)) is None
    assert save_raw_frame(frame, None) is None


def test_persist_event_saves_clean_frame_only_when_enabled(Session, world, tmp_path, monkeypatch):
    from app.ai import inference_worker as iw
    monkeypatch.setattr(iw.settings, "recordings_dir", str(tmp_path))
    frame = np.full((48, 64, 3), 120, dtype=np.uint8)
    ev = SimpleNamespace(detection_type="staff_present", zone_id=world["zone"], confidence=.9,
                         bbox_norm=[.1, .1, .5, .9], extra={"rule": "counter_unattended"},
                         cls=None)
    for enabled in (False, True):
        monkeypatch.setattr(iw.settings, "eval_capture_raw_frames", enabled)
        with Session() as db:
            rec_id, _ = iw._persist_event(db, world["cam"], ev, None, frame_bgr=frame)
            db.commit()
            rec = db.get(DetectionEvent, rec_id)
            raw = (rec.extra or {}).get("raw_snapshot_path")
            assert rec.thumbnail_path and os.path.isfile(rec.thumbnail_path)
            if enabled:
                assert raw and os.path.isfile(raw) and raw.endswith("_raw.jpg")
            else:
                assert raw is None


def test_prune_alerts_deletes_clean_frame_with_snapshot(Session, world, tmp_path, monkeypatch):
    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"s")
    raw = tmp_path / "snap_raw.jpg"
    raw.write_bytes(b"r")
    with Session() as db:
        ev = DetectionEvent(camera_id=world["cam"], detection_type="staff_present",
                            confidence=.9, bbox_json=[0, 0, 1, 1], thumbnail_path=str(snap),
                            extra={"raw_snapshot_path": str(raw)},
                            timestamp=datetime.now(timezone.utc) - timedelta(days=200))
        db.add(ev)
        db.flush()
        db.add(Alert(event_id=ev.id, status="new"))
        db.commit()
    monkeypatch.setattr("app.database.SessionLocal", Session)
    from app.tasks.maintenance import prune_alerts
    prune_alerts(retention_days=90)
    assert not snap.exists() and not raw.exists()
