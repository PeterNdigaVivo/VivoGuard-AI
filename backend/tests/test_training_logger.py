"""Tests for app.training.training_logger — dataset hashing + run records."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.training.training_logger import compute_dataset_hash, log_training_run


def _seed_dataset(root: Path) -> None:
    (root / "images").mkdir(parents=True)
    (root / "images" / "1.jpg").write_bytes(b"AAAA")
    (root / "labels").mkdir()
    (root / "labels" / "1.txt").write_text("0 0.5 0.5 0.4 0.4\n")
    (root / "train.txt").write_text(str(root / "images" / "1.jpg"))
    (root / "data.yaml").write_text("names: ['x']\n")


def test_hash_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _seed_dataset(a)
    _seed_dataset(b)
    # Same relative content → same hash even at different absolute roots…
    # (manifests embed absolute paths, so normalise them first)
    (b / "train.txt").write_text((a / "train.txt").read_text())
    assert compute_dataset_hash(a) == compute_dataset_hash(b)
    # …and a single changed byte changes it.
    (b / "images" / "1.jpg").write_bytes(b"AAAB")
    assert compute_dataset_hash(a) != compute_dataset_hash(b)


def test_hash_sensitive_to_renames_and_additions(tmp_path: Path) -> None:
    a = tmp_path / "a"
    _seed_dataset(a)
    h1 = compute_dataset_hash(a)
    (a / "labels" / "2.txt").write_text("")          # addition
    h2 = compute_dataset_hash(a)
    assert h1 != h2
    (a / "labels" / "2.txt").rename(a / "labels" / "3.txt")   # rename
    assert compute_dataset_hash(a) != h2


def test_hash_ignores_non_dataset_files(tmp_path: Path) -> None:
    a = tmp_path / "a"
    _seed_dataset(a)
    h1 = compute_dataset_hash(a)
    (a / "notes.log").write_text("scratch")          # not a hashed suffix
    assert compute_dataset_hash(a) == h1


def test_hash_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        compute_dataset_hash(tmp_path / "nope")


def test_log_training_run_record(tmp_path: Path) -> None:
    path = log_training_run(
        tmp_path / "runs", job_id=42, status="done",
        dataset_id=7, dataset_hash="ab" * 32, parent_model_id=3, model_id=9,
        hyperparameters={"epochs": 20, "batch": 16},
        augmentation_config={"mosaic": 0.5},
        sanitize_report={"kept": 100},
        final_validation_metrics={"map50": 0.91})
    record = json.loads(path.read_text())
    assert record["run_id"].startswith("run_42_")
    for key in ("timestamp", "job_id", "status", "dataset_hash",
                "parent_model_id", "hyperparameters", "augmentation_config",
                "sanitize_report", "final_validation_metrics"):
        assert key in record
    assert record["status"] == "done"
    assert record["final_validation_metrics"]["map50"] == 0.91
    assert record["error"] is None


def test_log_training_run_failed_record(tmp_path: Path) -> None:
    path = log_training_run(tmp_path / "runs", job_id=1, status="failed",
                            error="insufficient data: 12 < 50")
    record = json.loads(path.read_text())
    assert record["status"] == "failed"
    assert "insufficient data" in record["error"]
