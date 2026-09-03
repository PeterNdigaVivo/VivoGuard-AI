"""Regression tests for the cross-store dataset-prep failure
(jobs 807/808: "insufficient validation images: 0 < 5 (train=0,
test=0)" against a ds_ directory full of staged jpgs).

Root cause chain proven here against the REAL prep functions on an
in-memory SQLite DB:
  * prep never reads train.txt/val.txt from disk — it rebuilds them
    from TrainingImage rows; the on-disk jpgs/txts were stale copies
    from earlier successful runs.
  * the old build_cross_store_dataset COMMITTED its delete before the
    re-add loop, so an interruption left the dataset permanently
    empty → every subsequent job failed at prep with exactly the
    observed error.
The build is now atomic (delete + re-add in one transaction) and the
prep error names its failing stage.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Alert, Annotation, Camera, Dataset, DetectionEvent, Store, TrainingImage,
)

TABLES = [Store.__table__, Camera.__table__, DetectionEvent.__table__,
          Alert.__table__, Dataset.__table__, TrainingImage.__table__,
          Annotation.__table__]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path / "datasets"))
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=TABLES)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _seed_confirmed_pool(db, tmp_path, n=40):
    """n confirmed alerts across the three cross-store stores, each with
    a real jpg on disk — the source pool _cross_store_query selects."""
    from app.training.orchestrator import CROSS_STORE_STORES
    src_dir = tmp_path / "thumbs"
    src_dir.mkdir(parents=True, exist_ok=True)
    pool = Dataset(name="feedback-person", classes_json=["person"])
    db.add(pool)
    db.flush()
    for i in range(n):
        store_name = CROSS_STORE_STORES[i % len(CROSS_STORE_STORES)]
        store = db.query(Store).filter(Store.name == store_name).first()
        if store is None:
            store = Store(name=store_name, country="Kenya")
            db.add(store)
            db.flush()
        cam = Camera(name=f"cam{i}", store_id=store.id, brand="dahua",
                     connection_type="rtsp", host="10.0.0.1")
        db.add(cam)
        db.flush()
        ev = DetectionEvent(camera_id=cam.id, detection_type="person",
                            confidence=0.9, bbox_json=[0.1, 0.1, 0.5, 0.5],
                            timestamp=datetime.now(timezone.utc))
        db.add(ev)
        db.flush()
        a = Alert(event_id=ev.id, status="confirmed",
                  feedback_used_for_training=True)
        db.add(a)
        db.flush()
        f = src_dir / f"{i}.jpg"
        f.write_bytes(b"\xff\xd8\xff" + b"0" * 128)
        db.add(TrainingImage(dataset_id=pool.id, camera_id=cam.id,
                             file_path=str(f), labeled=True,
                             source_alert_id=a.id))
    db.commit()


def test_healthy_build_split_and_yaml(db, tmp_path):
    """End-to-end prep against real files: build -> split -> yaml."""
    from app.training.dataset import split_dataset, write_yolo_dataset_yaml
    from app.training.orchestrator import build_cross_store_dataset

    _seed_confirmed_pool(db, tmp_path, n=40)
    res = build_cross_store_dataset(db)
    assert res["total_images"] == 40 and res["dataset_id"]
    ds_id = res["dataset_id"]

    split_dataset(db, ds_id, train=0.8, val=0.2, test=0.0)
    yaml_path = write_yolo_dataset_yaml(db, ds_id)
    root = yaml_path.parent
    train = [x for x in (root / "train.txt").read_text().splitlines() if x]
    val = [x for x in (root / "val.txt").read_text().splitlines() if x]
    assert len(train) == 32 and len(val) == 8
    assert all(os.path.exists(p) for p in train + val)


def test_empty_dataset_error_names_the_stage(db, tmp_path):
    """The jobs-807/808 shape: rows gone, stale files on disk — the
    error must say the DB selection was empty, not just '0 < 5'."""
    from app.training.dataset import (
        dataset_root, split_dataset, write_yolo_dataset_yaml,
    )
    ds = Dataset(name="vivo_cross_store_v1", classes_json=["person"])
    db.add(ds)
    db.commit()
    # Stale artifacts from a previous successful run.
    root = dataset_root(ds.id)
    (root / "images" / "999.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "train.txt").write_text("stale")
    split_dataset(db, ds.id)
    with pytest.raises(ValueError) as e:
        write_yolo_dataset_yaml(db, ds.id)
    msg = str(e.value)
    assert "insufficient validation images: 0 < 5" in msg
    assert "db-selected=0" in msg
    assert "Stage: DB selection" in msg


def test_rebuild_is_atomic_on_interruption(db, tmp_path, monkeypatch):
    """An exception mid-re-add must NOT leave the dataset empty. The
    old code committed the delete first — this test fails on it."""
    from app.training import orchestrator as orch

    _seed_confirmed_pool(db, tmp_path, n=40)
    first = orch.build_cross_store_dataset(db)
    ds_id = first["dataset_id"]
    before = (db.query(TrainingImage)
                .filter(TrainingImage.dataset_id == ds_id).count())
    assert before == 40

    # Second rebuild dies partway through the re-add loop.
    calls = {"n": 0}
    real_flush = db.flush

    def exploding_flush(*a, **k):
        calls["n"] += 1
        if calls["n"] == 10:
            raise RuntimeError("simulated crash mid-rebuild")
        return real_flush(*a, **k)

    monkeypatch.setattr(db, "flush", exploding_flush)
    with pytest.raises(RuntimeError):
        orch.build_cross_store_dataset(db)
    monkeypatch.setattr(db, "flush", real_flush)
    db.rollback()

    after = (db.query(TrainingImage)
               .filter(TrainingImage.dataset_id == ds_id).count())
    assert after == before, (
        "interrupted rebuild emptied the dataset — the delete must "
        "commit together with the re-add")


def test_quarantined_dataset_error_names_provenance_stage(db, tmp_path):
    """Rows exist but migration-0042-style quarantine excludes them —
    the error must name the provenance stage, not just '0 < 5'."""
    from app.training.dataset import split_dataset, write_yolo_dataset_yaml
    ds = Dataset(name="vivo_cross_store_v1", classes_json=["person"])
    db.add(ds)
    db.flush()
    src = tmp_path / "q.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    for i in range(10):
        db.add(TrainingImage(dataset_id=ds.id, file_path=str(src),
                             labeled=True, eligible_for_training=False,
                             review_state="pending"))
    db.commit()
    split_dataset(db, ds.id)
    with pytest.raises(ValueError) as e:
        write_yolo_dataset_yaml(db, ds.id)
    msg = str(e.value)
    assert "db-selected=10" in msg and "provenance-eligible=0" in msg
    assert "Stage: provenance" in msg and "0045" in msg


def test_training_provenance_honours_policy_flag(monkeypatch):
    from app.config import settings
    from app.training.feedback_loop import _training_provenance
    monkeypatch.setattr(settings, "training_require_dual_review", False)
    p = _training_provenance("correct")
    assert p["eligible_for_training"] is True and p["review_state"] == "approved"
    assert p["source_kind"] == "operator_confirmed"
    n = _training_provenance("false")
    assert n["eligible_for_training"] is True and n["source_kind"] == "operator_dismissed"
    monkeypatch.setattr(settings, "training_require_dual_review", True)
    q = _training_provenance("correct")
    assert q["eligible_for_training"] is False and q["review_state"] == "pending"
