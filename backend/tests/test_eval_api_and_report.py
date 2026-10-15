"""Admin labelling API, report building/rendering and GPU summary (app/eval)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.deps import get_current_user
from app.eval import api as eval_api
from app.eval import gpu_sample, run
from app.models import EvalSample
from app.utils.system_admins import SYSTEM_ADMIN_EMAILS

ADMIN = sorted(SYSTEM_ADMIN_EMAILS)[0]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    root = tmp_path / "eval"
    (root / "frames").mkdir(parents=True)
    monkeypatch.setattr(eval_api.settings, "eval_dir", str(root))
    frame = root / "frames" / "people_1.jpg"
    frame.write_bytes(b"jpeg")
    with Session() as db:
        db.add_all([
            EvalSample(kind="people", camera_id=1, frame_path=str(frame), status="pending"),
            EvalSample(kind="clip", camera_id=1, clip_path=str(tmp_path / "outside.mp4"),
                       status="pending"),
            EvalSample(kind="alert", category="person", verdict="true", status="labelled"),
            EvalSample(kind="alert", category="person", verdict="false", status="labelled"),
        ])
        db.commit()
    (tmp_path / "outside.mp4").write_bytes(b"mp4")

    def client(email):
        app = FastAPI()
        app.include_router(eval_api.router)

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = (
            lambda: SimpleNamespace(id=9, email=email, role="admin", is_active=True))
        return TestClient(app)
    return {"client": client, "Session": Session}


def test_non_system_admins_are_refused(env):
    c = env["client"]("manager@example.test")          # a store admin, not a system admin
    for method, url in (("get", "/eval/summary"), ("get", "/eval/next?kind=people"),
                        ("get", "/eval/samples/1/media"), ("post", "/eval/samples/1/label")):
        r = c.post(url, json={"skip": True}) if method == "post" else c.get(url)
        assert r.status_code == 403, url


def test_label_people_flow(env):
    c = env["client"](ADMIN)
    nxt = c.get("/eval/next?kind=people").json()
    assert nxt["kind"] == "people" and nxt["media_type"] == "image/jpeg"
    assert c.get(f"/eval/samples/{nxt['id']}/media").content == b"jpeg"
    bad = c.post(f"/eval/samples/{nxt['id']}/label", json={"points": [[1.5, 0.2]]})
    assert bad.status_code == 422
    ok = c.post(f"/eval/samples/{nxt['id']}/label", json={"points": [[0.2, 0.3], [0.7, 0.8]]})
    assert ok.json() == {"id": nxt["id"], "status": "labelled"}
    assert c.get("/eval/next?kind=people").json() is None
    with env["Session"]() as db:
        s = db.get(EvalSample, nxt["id"])
        assert s.label == {"points": [[0.2, 0.3], [0.7, 0.8]]} and s.labelled_by == 9
    summary = c.get("/eval/summary").json()
    assert summary["alerts"]["person"] == {"total": 2, "true": 1, "false": 1}
    assert summary["people"] == {"labelled": 1}


def test_clip_count_and_skip(env):
    c = env["client"](ADMIN)
    clip = c.get("/eval/next?kind=clip").json()
    assert c.post(f"/eval/samples/{clip['id']}/label", json={}).status_code == 422
    assert c.post(f"/eval/samples/{clip['id']}/label", json={"skip": True}).json()["status"] == "skipped"


def test_files_outside_the_eval_dir_are_never_served(env):
    c = env["client"](ADMIN)
    with env["Session"]() as db:
        clip_id = db.query(EvalSample).filter_by(kind="clip").one().id
    assert c.get(f"/eval/samples/{clip_id}/media").status_code == 404


# ── report ─────────────────────────────────────────────────────────────

COUNTER = [[0.0, 0.5], [0.5, 0.5], [0.5, 1.0], [0.0, 1.0]]


def _s(i, **kw):
    return SimpleNamespace(id=i, frame_path=f"f{i}.jpg", **kw)


def test_evaluate_detector_scores_alerts_and_people():
    samples = {
        "alerts": [
            # Operator: False (someone WAS at the counter) — model sees them -> avoided.
            _s(1, category="counter_unattended", verdict="false", zone_polygon=COUNTER),
            # Operator: True (really unattended) — model sees nobody -> kept.
            _s(2, category="counter_unattended", verdict="true", zone_polygon=COUNTER),
            # Operator: True intrusion — model sees nobody -> missed.
            _s(3, category="person", verdict="true", zone_polygon=None),
        ],
        "people": [_s(4, label={"points": [[0.2, 0.7], [0.9, 0.1]]})],
        "uniform": [],
    }
    person_at_counter = [{"cls": "person", "conf": 0.9, "bbox_norm": [0.1, 0.6, 0.3, 0.9]}]
    outputs = {"f1.jpg": person_at_counter, "f2.jpg": [], "f3.jpg": [],
               "f4.jpg": person_at_counter}
    current = {}

    def read(path):
        current["path"] = path
        return path

    res = run.evaluate_detector(samples, lambda frame: outputs[frame], conf=0.25,
                                read=read, memory_fn=lambda: 100.0)
    counter = res["alerts"]["counter_unattended"]
    assert (counter["false_alert_avoided"], counter["real_alert_kept"]) == (1, 1)
    assert res["alerts"]["person"]["real_alert_missed"] == 1
    assert res["people"]["people_found"] == 1 and res["people"]["people_missed"] == 1
    assert res["speed"]["frames"] == 4 and res["gpu_memory_mb"] == 0.0 and res["errors"] == 0


def test_evaluate_uniform_and_failures_are_counted():
    samples = [_s(1, verdict="true", event_bbox=[0, 0, 1, 1]),
               _s(2, verdict="false", event_bbox=[0, 0, 1, 1]),
               _s(3, verdict="true", event_bbox=[0, 0, 1, 1])]
    states = {"f1.jpg": "non_compliant", "f2.jpg": "full_compliant"}

    def method(frame, bbox):
        if frame not in states:
            raise OSError("unreadable")
        return states[frame]
    res = run.evaluate_uniform(samples, method, read=lambda p: p)
    u = res["alerts"]["uniform"]
    assert (u["real_alert_kept"], u["false_alert_avoided"], res["errors"]) == (1, 1, 1)


def test_render_has_one_column_per_model_and_shows_previous():
    det = {"alerts": {"person": {"real_alerts_kept_pct": 80.0, "false_alerts_avoided_pct": 60.0}},
           "people": {"found_pct": 70.0, "people_missed": 3, "false_detections": 1,
                      "edge_found_pct": 40.0},
           "speed": {"median_ms": 1.5, "p95_ms": 2.0}, "gpu_memory_mb": 300.0}
    report = {"created_at": "2026-10-15 09:00 UTC", "conf": 0.25, "imgsz": 640,
              "environment": {"gpu": "Test GPU"},
              "test_set": {"alerts": {"person": {"true": 5, "false": 5}}, "people_frames": 2,
                           "people_clicked": 9},
              "detectors": {"yolov8n.pt": det, "yolo11m.pt": det}, "uniform": {},
              "snapshot_volume": {"days": 30, "median_per_day": 120, "max_per_day": 300}}
    text = run.render(report, previous={"detectors": {"yolov8n.pt": det}})
    assert "| Detector | yolov8n.pt | yolo11m.pt |" in text
    assert "80 (was 80)" in text and "| person: real alerts kept % |" in text
    assert "Alert snapshots/day" in text


def test_gpu_summary_finds_busiest_hour_and_headroom():
    base = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc).timestamp()   # 12:00 Nairobi
    samples = [{"ts": base + i * 60, "gpu_util_pct": u, "mem_used_mb": m, "mem_total_mb": 16000}
               for i, (u, m) in enumerate([(40, 6000), (60, 7000), (90, 9000)])]
    samples.append({"ts": base + 3 * 3600, "gpu_util_pct": 10, "mem_used_mb": 5000,
                    "mem_total_mb": 16000})
    s = gpu_sample.summarise(samples)
    assert s["gpu_util_peak_pct"] == 90 and s["mem_free_min_mb"] == 7000
    assert s["busiest_hour_eat"] == "12:00"
    assert "at least 7000 MB free" in gpu_sample.render(s)
    assert gpu_sample.summarise([]) == {"samples": 0}
