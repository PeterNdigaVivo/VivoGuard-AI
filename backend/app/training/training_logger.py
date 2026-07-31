"""Local experiment tracking — one structured JSON log per training run.

No external service (W&B / MLflow) is configured for this deployment, so
every run writes a self-contained JSON record under
`<models_dir>/../training_runs/` (override via TRAINING_RUNS_DIR). The
record carries everything needed to reproduce or audit the run:

    run_id, timestamp, job_id, parent_model_id, dataset_id,
    dataset_hash, hyperparameters, augmentation_config,
    sanitize_report, final_validation_metrics, status, error

`dataset_hash` is a SHA256 over the sorted (relative-path, content-hash)
pairs of the staged dataset directory — two runs with byte-identical
data produce the same hash, giving basic dataset versioning without a
data-lake dependency.

This module is deliberately free of app.config imports so it stays unit-
testable without the full settings stack — callers pass the directory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# File types that constitute "the dataset" for hashing purposes: images,
# YOLO labels, manifests, and the data.yaml itself.
_HASHED_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".txt", ".yaml"})


def compute_dataset_hash(dataset_path: str | Path) -> str:
    """SHA256 fingerprint of a staged dataset directory.

    Hashes the sorted stream of (relative path, per-file SHA256) pairs so
    both content changes AND file renames/additions/removals change the
    fingerprint. Returns the hex digest; raises ValueError when the
    directory does not exist."""
    root = Path(dataset_path)
    if not root.is_dir():
        raise ValueError(f"dataset path does not exist: {root}")
    outer = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _HASHED_SUFFIXES:
            continue
        inner = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                inner.update(chunk)
        outer.update(str(p.relative_to(root)).encode())
        outer.update(inner.hexdigest().encode())
    return outer.hexdigest()


def log_training_run(runs_dir: str | Path, *,
                     job_id: int,
                     status: str,
                     dataset_id: int | None = None,
                     dataset_hash: str | None = None,
                     parent_model_id: int | None = None,
                     model_id: int | None = None,
                     hyperparameters: dict[str, Any] | None = None,
                     augmentation_config: dict[str, Any] | None = None,
                     sanitize_report: dict[str, Any] | None = None,
                     final_validation_metrics: dict[str, Any] | None = None,
                     error: str | None = None) -> Path:
    """Write one JSON record for a training run and return its path.

    Raises on I/O failure — a run that cannot be recorded should be loud,
    not silent (the record IS the audit trail)."""
    runs = Path(runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    run_id = f"run_{job_id}_{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "run_id":                   run_id,
        "timestamp":                datetime.now(timezone.utc).isoformat(),
        "job_id":                   job_id,
        "status":                   status,          # done | failed
        "dataset_id":               dataset_id,
        "dataset_hash":             dataset_hash,
        "parent_model_id":          parent_model_id,
        "model_id":                 model_id,
        "hyperparameters":          hyperparameters or {},
        "augmentation_config":      augmentation_config or {},
        "sanitize_report":          sanitize_report or {},
        "final_validation_metrics": final_validation_metrics or {},
        "error":                    error,
    }
    path = runs / f"{run_id}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    log.info("training run logged: %s (job=%s status=%s dataset_hash=%s)",
             path, job_id, status, (dataset_hash or "")[:12])
    return path
