"""Governed evidence capture for the live-camera simulation probe.

The deterministic scenario catalogue is synthetic and must never enter model
training.  This module handles the separate live probe: it persists a small,
deduplicated sample of real cached camera frames so humans can review detector
behaviour.  Every sample is fail-closed until an independent reviewer approves
the primary annotation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import Annotation, AssuranceCase, Camera, TrainingImage
from app.training.dataset import save_uploaded_image
from app.training.feedback_loop import _ensure_dataset


LIVE_PROBE_SOURCE = "simulation_live_probe"
LIVE_PROBE_DATASET = "feedback-person"


@dataclass(frozen=True)
class LiveProbeCandidate:
    camera_id: int
    jpeg: bytes
    detections: list[dict[str, Any]]
    captured_at: datetime


def _person_suggestions(detections: list[dict[str, Any]]) -> list[list[float]]:
    suggestions: list[list[float]] = []
    for detection in detections:
        if str(detection.get("cls", "")).lower() not in {"person", "people", "human"}:
            continue
        bbox = detection.get("bbox_norm") or []
        if len(bbox) != 4:
            continue
        x1, y1, x2, y2 = (max(0.0, min(1.0, float(value))) for value in bbox)
        if x2 <= x1 or y2 <= y1:
            continue
        suggestions.append([
            (x1 + x2) / 2.0,
            (y1 + y2) / 2.0,
            x2 - x1,
            y2 - y1,
        ])
    return suggestions


def capture_live_probe_evidence(
    db: Session,
    candidates: list[LiveProbeCandidate],
    *,
    run_id: str,
    max_per_run: int = 10,
    dedupe_days: int = 7,
    now: datetime | None = None,
) -> dict[str, int]:
    """Persist bounded real-frame evidence, quarantined for human review.

    One sample per camera/result class (person seen vs no person seen) is kept
    inside the dedupe window. Frames with no person detection are prioritised
    because they are the best candidates for finding false negatives.
    """
    limit = max(0, int(max_per_run))
    if limit == 0 or not candidates:
        return {"created": 0, "deduplicated": 0, "invalid": 0}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(dedupe_days)))
    camera_ids = sorted({int(candidate.camera_id) for candidate in candidates})
    recent = (db.query(TrainingImage)
              .filter(TrainingImage.camera_id.in_(camera_ids),
                      TrainingImage.source_kind == "simulation",
                      TrainingImage.created_at >= cutoff)
              .all())
    seen = {
        (int(row.camera_id), str((row.source_extra or {}).get("probe_result")))
        for row in recent if row.camera_id is not None
    }

    prepared: list[tuple[LiveProbeCandidate, list[list[float]], str]] = []
    invalid = 0
    for candidate in candidates:
        if not candidate.jpeg:
            invalid += 1
            continue
        suggestions = _person_suggestions(candidate.detections)
        result = "person_detected" if suggestions else "no_person_detected"
        prepared.append((candidate, suggestions, result))
    prepared.sort(key=lambda item: (bool(item[1]), int(item[0].camera_id)))

    dataset = _ensure_dataset(
        db,
        LIVE_PROBE_DATASET,
        ["person"],
        "Real camera frames from detector probes; independent review required.",
    )
    created_paths: list[Path] = []
    created = 0
    deduplicated = 0
    camera_store_ids = {
        int(camera.id): camera.store_id
        for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all()
    }
    try:
        for candidate, suggestions, result in prepared:
            key = (int(candidate.camera_id), result)
            if key in seen:
                deduplicated += 1
                continue
            if created >= limit:
                break
            filename = (
                f"sim_{run_id}_cam{int(candidate.camera_id)}_{result}.jpg"
            )
            path = save_uploaded_image(dataset.id, filename, candidate.jpeg)
            created_paths.append(path)
            image = TrainingImage(
                dataset_id=dataset.id,
                camera_id=int(candidate.camera_id),
                file_path=str(path),
                captured_at=candidate.captured_at,
                labeled=False,
                source_kind="simulation",
                eligible_for_training=False,
                review_state="quarantined",
                simulation_run_id=run_id,
                source_extra={
                    "source": LIVE_PROBE_SOURCE,
                    "execution_mode": "live_camera_replay",
                    "synthetic": False,
                    "probe_result": result,
                    "model_confidence_threshold": 0.25,
                    "human_review_required": True,
                    "independent_review_required": True,
                    "suggestion_count": len(suggestions),
                },
            )
            db.add(image)
            db.flush()
            db.add(AssuranceCase(
                dedup_key=f"simulation-evidence:{image.id}",
                case_type="simulation_evidence_review",
                severity="medium",
                status="open",
                store_id=camera_store_ids.get(int(candidate.camera_id)),
                camera_id=int(candidate.camera_id),
                title=f"Review live simulation evidence #{image.id}",
                description=(
                    "Real camera probe frame; complete primary labelling, "
                    "then hand it to an independent reviewer."
                ),
                evidence={
                    "training_image_id": image.id,
                    "simulation_run_id": run_id,
                    "review_sla_minutes": 30,
                },
                training_status="pending_primary_review",
                human_review_required=True,
            ))
            for bbox in suggestions:
                db.add(Annotation(
                    image_id=image.id,
                    class_label="person",
                    bbox_json=bbox,
                    verified=False,
                    auto_suggested=True,
                ))
            seen.add(key)
            created += 1
        db.commit()
    except Exception:
        db.rollback()
        for path in created_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {"created": created, "deduplicated": deduplicated, "invalid": invalid}
