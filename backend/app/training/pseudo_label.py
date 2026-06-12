"""Batch pseudo-labelling.

Runs the deployed production YOLO model against unlabelled
TrainingImage rows and writes its high-confidence detections as
Annotations (`auto_suggested=True`). Low-confidence frames are
left for human review; only detections above PSEUDO_LABEL_CONF are
marked `verified=True` and become trainer-eligible immediately.

Standard FixMatch-style threshold: 0.75 catches most genuine hits
while keeping noisy class confusion out of the training set. The
threshold + which model to use are configurable per call.

Operator can still override / delete pseudo-labels via the normal
annotation UI before the next fine-tune runs.
"""
from __future__ import annotations
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import AIModel, Annotation, TrainingImage

log = logging.getLogger(__name__)

# Tuned default — high enough to skip class-confusion noise, low
# enough that a moderately-trained model still produces useful
# self-labels at scale.
PSEUDO_LABEL_CONF = 0.75


def _resolve_pseudo_model_weights(db: Session) -> str | None:
    """Pick the weights the pseudo-labeller should use. Prefers a
    deployed chain model; falls back to any deployed model; falls
    back to None (which makes the YOLO runner load the bundled base
    weights — still useful for common classes like 'person')."""
    m = (db.query(AIModel)
           .filter(AIModel.deployed == True,                       # noqa: E712
                   AIModel.is_chain_model == True)                 # noqa: E712
           .order_by(AIModel.created_at.desc())
           .first())
    if m is None:
        m = (db.query(AIModel)
               .filter(AIModel.deployed == True)                   # noqa: E712
               .order_by(AIModel.created_at.desc())
               .first())
    return m.weights_path if m else None


def pseudo_label_image(db: Session, image_id: int, *,
                       conf: float = PSEUDO_LABEL_CONF,
                       weights: str | None = None) -> int:
    """Run pseudo-labelling on a single image. Returns the number of
    annotations written (0 if the image was already labelled, the
    file is missing, or nothing met the threshold)."""
    img = db.get(TrainingImage, image_id)
    if img is None or img.labeled:
        return 0
    if not Path(img.file_path).exists():
        # Skip + mark labelled so we don't retry forever — operator
        # can re-upload if needed.
        img.labeled = True
        db.commit()
        return 0

    # Local import keeps numpy/PIL/ultralytics out of the slim API image.
    from app.training.annotation import auto_suggest
    hits = auto_suggest(img.file_path, weights=weights, conf=conf)
    if not hits:
        # No high-confidence detections — leave the image alone for a
        # human to label. Don't flip labeled=True; this image may
        # become useful once a stronger model trains.
        return 0

    for h in hits:
        db.add(Annotation(
            image_id=img.id,
            class_label=h["class_label"],
            bbox_json=h["bbox_json"],
            auto_suggested=True,
            verified=True,           # high-conf → trusted into training
        ))
    img.labeled = True
    db.commit()
    log.info("pseudo-label: image=%s hits=%d conf>=%.2f", img.id, len(hits), conf)
    return len(hits)


def pseudo_label_dataset(db: Session, dataset_id: int, *,
                         conf: float = PSEUDO_LABEL_CONF,
                         weights: str | None = None,
                         limit: int = 500) -> dict:
    """Walk up to `limit` unlabelled images in `dataset_id` and
    pseudo-label them. Returns {processed, labelled, total_annotations}."""
    pending = (db.query(TrainingImage)
                 .filter(TrainingImage.dataset_id == dataset_id,
                         TrainingImage.labeled == False)           # noqa: E712
                 .limit(int(limit))
                 .all())
    processed = 0
    labelled  = 0
    total     = 0
    if weights is None:
        weights = _resolve_pseudo_model_weights(db)
    for img in pending:
        processed += 1
        n = pseudo_label_image(db, img.id, conf=conf, weights=weights)
        if n:
            labelled += 1
            total    += n
    return {"processed": processed, "labelled": labelled,
            "total_annotations": total, "weights": weights}


def pseudo_label_all_pending(db: Session, *,
                              conf: float = PSEUDO_LABEL_CONF,
                              limit_per_dataset: int = 500) -> dict:
    """Walk every Dataset whose name starts with `feedback-` (the
    auto-collected pools) plus any dataset that has unlabelled
    images. Used by the hourly Celery task."""
    from app.models import Dataset
    weights = _resolve_pseudo_model_weights(db)
    ds_rows = (db.query(Dataset.id)
                  .join(TrainingImage, TrainingImage.dataset_id == Dataset.id)
                  .filter(TrainingImage.labeled == False)           # noqa: E712
                  .distinct()
                  .all())
    summary = {"datasets": 0, "labelled": 0, "total_annotations": 0,
               "weights": weights}
    for (ds_id,) in ds_rows:
        r = pseudo_label_dataset(db, ds_id, conf=conf, weights=weights,
                                  limit=limit_per_dataset)
        summary["datasets"]          += 1
        summary["labelled"]          += r["labelled"]
        summary["total_annotations"] += r["total_annotations"]
    return summary
