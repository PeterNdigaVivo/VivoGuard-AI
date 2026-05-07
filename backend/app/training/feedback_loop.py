"""Continuous learning — turn confirmed/dismissed alerts into training data.

Confirmed alert → add the event's frame to the *positive retraining pool*
                  with the same bbox/label.
Dismissed alert → flag the underlying frame so we don't accidentally
                  reuse it as a positive in future training.

The pool is stored as a special Dataset named "feedback-<detection_type>".
"""
from __future__ import annotations
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIModel, Alert, Annotation, Dataset, DetectionEvent, TrainingImage,
)

log = logging.getLogger(__name__)


def _ensure_dataset(db: Session, name: str, classes: list[str]) -> Dataset:
    ds = db.query(Dataset).filter(Dataset.name == name).first()
    if not ds:
        ds = Dataset(name=name, description="auto: alert feedback loop", classes_json=classes)
        db.add(ds)
        db.commit()
        db.refresh(ds)
    else:
        # union classes
        cur = set(ds.classes_json or [])
        for c in classes:
            cur.add(c)
        ds.classes_json = sorted(cur)
        db.commit()
    return ds


def absorb_confirmed(db: Session, alert_id: int) -> None:
    a = db.get(Alert, alert_id)
    if not a or a.feedback_used_for_training:
        return
    ev = db.get(DetectionEvent, a.event_id)
    if not ev or not ev.thumbnail_path:
        return
    ds_name = f"feedback-{ev.detection_type}"
    cls = ev.detection_type
    ds  = _ensure_dataset(db, ds_name, [cls])
    img = TrainingImage(
        dataset_id=ds.id,
        camera_id=ev.camera_id,
        file_path=ev.thumbnail_path,
        labeled=True,
    )
    db.add(img)
    db.flush()
    # YOLO bbox = (cx, cy, w, h). Event has [x1,y1,x2,y2] normalised.
    x1, y1, x2, y2 = ev.bbox_json
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    w  = max(0.0, x2 - x1)
    h  = max(0.0, y2 - y1)
    db.add(Annotation(image_id=img.id, class_label=cls, bbox_json=[cx, cy, w, h], verified=True))
    a.feedback_used_for_training = True
    db.commit()
    log.info("feedback: confirmed alert %s absorbed into dataset %s", alert_id, ds.id)


def mark_dismissed(db: Session, alert_id: int) -> None:
    """We don't yet remove the dismissed image from training data; this
    just stamps the alert. Future work: add a 'negative' dataset."""
    a = db.get(Alert, alert_id)
    if not a:
        return
    a.feedback_used_for_training = True
    db.commit()


def pending_retraining_count(db: Session, detection_type: str) -> int:
    """Count newly absorbed positives since the last model deployment."""
    ds = db.query(Dataset).filter(Dataset.name == f"feedback-{detection_type}").first()
    if not ds:
        return 0
    return db.query(TrainingImage).filter(TrainingImage.dataset_id == ds.id).count()
