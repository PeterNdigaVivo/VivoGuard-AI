"""Continuous learning — turn confirmed/dismissed alerts into training data.

Confirmed alert → add the event's frame to the *positive retraining pool*
                  with the same bbox/label.
Dismissed alert → add the event's frame to the *negative retraining pool*
                  (TrainingImage with NO Annotation rows = explicit
                  background sample in YOLO format), so the next
                  fine-tune learns "this is NOT a {detection_type}".

Both pools are stored as Datasets named:
  feedback-<detection_type>              ← positives (legacy name kept)
  feedback-negative-<detection_type>     ← hard negatives (new)

Naming convention is deliberate: the trainer joins both pools into
one dataset for fine-tuning, with negatives kept at a max of
3 × positives (the standard hard-negative ratio that avoids
swamping the gradient with background examples).
"""
from __future__ import annotations
import logging

from sqlalchemy.orm import Session

from app.models import (
    Alert, Annotation, Dataset, DetectionEvent, TrainingImage,
)

log = logging.getLogger(__name__)


def _build_source_extra(db: Session, ev: DetectionEvent,
                         alert: Alert, verdict: str) -> dict:
    """Snapshot every piece of evidence the spec asks us to persist
    next to the feedback frame: alert id + verdict, detection type,
    store + camera + zone names, timestamp, confidence, bbox, plus
    whatever sub-payload the detector dumped into DetectionEvent.extra
    (person_count, tracking_ids, …)."""
    from app.models import Camera, Zone, Store
    cam = db.get(Camera, ev.camera_id) if ev.camera_id else None
    zone = db.get(Zone, ev.zone_id) if ev.zone_id else None
    store_id = getattr(cam, "store_id", None) if cam else None
    store_name = None
    if store_id:
        s = db.get(Store, store_id)
        store_name = s.name if s else None
    ts = ev.timestamp
    return {
        "alert_id":         alert.id,
        "event_id":         ev.id,
        "verdict":          verdict,        # "correct" | "false"
        "detection_type":   ev.detection_type,
        "camera_id":        ev.camera_id,
        "camera_name":      cam.name if cam else None,
        "store_id":         store_id,
        "store_name":       store_name,
        "zone_id":          ev.zone_id,
        "zone_name":        zone.name if zone else None,
        "confidence":       ev.confidence,
        "bbox_norm":        ev.bbox_json,
        "timestamp_iso":    ts.isoformat() if ts else None,
        "model_id":         ev.model_id,
        # DetectionEvent.extra already carries detector-specific
        # context (person_count, tracking_ids, queue_length, …) —
        # carry it through unchanged so curators can dedup/filter
        # without re-joining back to the event row.
        "detector_extra":   ev.extra or {},
    }


def _ensure_dataset(db: Session, name: str, classes: list[str],
                    description: str) -> Dataset:
    ds = db.query(Dataset).filter(Dataset.name == name).first()
    if not ds:
        ds = Dataset(name=name, description=description, classes_json=classes)
        db.add(ds)
        db.commit()
        db.refresh(ds)
        return ds
    # Union classes — adding a new detection sub-class shouldn't shrink the
    # set of labels the dataset already knows about.
    if classes:
        cur = set(ds.classes_json or [])
        for c in classes:
            cur.add(c)
        ds.classes_json = sorted(cur)
        db.commit()
    return ds


def absorb_confirmed(db: Session, alert_id: int) -> None:
    """Operator confirmed the alert was a true positive — promote the
    frame to the positive pool with its detection bbox as the ground-
    truth annotation."""
    a = db.get(Alert, alert_id)
    if not a or a.feedback_used_for_training:
        return
    ev = db.get(DetectionEvent, a.event_id)
    if not ev or not ev.thumbnail_path:
        return
    cls = ev.detection_type
    # Legacy name without the `-positive-` infix is kept so feedback
    # absorbed by earlier deploys lands in the same Dataset.
    ds  = _ensure_dataset(
        db, f"feedback-{cls}",
        [cls],
        description="auto: confirmed alerts (positive feedback pool)",
    )
    img = TrainingImage(
        dataset_id=ds.id,
        camera_id=ev.camera_id,
        file_path=ev.thumbnail_path,
        labeled=True,
        source_extra=_build_source_extra(db, ev, a, "correct"),
    )
    db.add(img); db.flush()
    # YOLO bbox = (cx, cy, w, h). Event has [x1,y1,x2,y2] normalised.
    x1, y1, x2, y2 = ev.bbox_json
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    w  = max(0.0, x2 - x1)
    h  = max(0.0, y2 - y1)
    db.add(Annotation(image_id=img.id, class_label=cls,
                       bbox_json=[cx, cy, w, h], verified=True))
    a.feedback_used_for_training = True
    db.commit()
    log.info("feedback: confirmed alert %s → positive pool %s", alert_id, ds.id)


def absorb_dismissed(db: Session, alert_id: int) -> None:
    """Operator dismissed the alert as a false positive — promote the
    frame to the *negative* pool. YOLO treats a TrainingImage row
    with no Annotation rows as a pure background sample; the next
    fine-tune learns to suppress whatever spurious feature triggered
    this detection."""
    a = db.get(Alert, alert_id)
    if not a or a.feedback_used_for_training:
        return
    ev = db.get(DetectionEvent, a.event_id)
    if not ev or not ev.thumbnail_path:
        # Stamp the alert anyway — we don't want to keep retrying
        # an alert whose snapshot was never saved.
        a.feedback_used_for_training = True
        db.commit()
        return
    cls = ev.detection_type
    ds  = _ensure_dataset(
        db, f"feedback-negative-{cls}",
        [],     # no classes — pure background
        description="auto: dismissed alerts (hard-negative pool)",
    )
    db.add(TrainingImage(
        dataset_id=ds.id,
        camera_id=ev.camera_id,
        file_path=ev.thumbnail_path,
        labeled=True,           # labelled as background — no Annotation rows
        source_extra=_build_source_extra(db, ev, a, "false"),
    ))
    a.feedback_used_for_training = True
    db.commit()
    log.info("feedback: dismissed alert %s → hard-negative pool %s",
             alert_id, ds.id)


# Backwards-compat alias — old call sites still import `mark_dismissed`.
def mark_dismissed(db: Session, alert_id: int) -> None:
    absorb_dismissed(db, alert_id)


def pending_retraining_count(db: Session, detection_type: str) -> dict:
    """How many positives + negatives are waiting in the feedback pool
    for this detection type. Used by the metrics endpoint and the
    chain-retrain trigger to decide if there's enough new signal to
    justify a fine-tune."""
    out = {"positives": 0, "negatives": 0}
    pairs = (("positives", f"feedback-{detection_type}"),
             ("negatives", f"feedback-negative-{detection_type}"))
    for kind, ds_name in pairs:
        ds = db.query(Dataset).filter(Dataset.name == ds_name).first()
        if ds is not None:
            out[kind] = (db.query(TrainingImage)
                            .filter(TrainingImage.dataset_id == ds.id).count())
    return out
