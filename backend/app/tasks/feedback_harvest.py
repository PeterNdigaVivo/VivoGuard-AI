"""Feedback-driven training harvesters (Aug 2026 mission).

Runs on the WORKER (opencv + the recordings volume live here; the API
container has neither):

  • harvest_temporal_frames — for every CONFIRMED alert, extract the
    frames ±1s around the detection moment from the alert clip and add
    them to the positive pool as labeled=False rows. The hourly
    training.pseudo_label_pending task then stamps REAL YOLO person
    boxes on them — the detection-moment bbox is never copied onto
    frames where the subject has moved.

  • run_shop_opening_specialist — build vivo_store_opening_motion_v1
    (10 frames spanning -30s..+5s of each confirmed shop_open_close
    clip, phase recorded in source_extra) and queue a 30-epoch
    fine-tune from the deployed parent with lighting-heavy
    augmentation.

  • run_store_specialist — build a per-store person/intrusion dataset
    from that store's feedback pools and queue a 15-epoch fine-tune;
    cfg.assign_store_id makes the trainer point that store's cameras
    at the new model (cameras.ai_model_id) once the model gate passes.

Frame-position phases (approaching / at_door / crossing / entering)
are stored in source_extra, NOT as YOLO classes: no ground-truth
boxes exist for them, and inventing class labels without annotations
would poison the detector (same principle as the mannequin
label_hint).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Clip layout written by tasks/recorder._extract_one: seconds of video
# BEFORE the event timestamp, per detection type.
_CLIP_PRE_S = {"shop_open_close": 45}
_CLIP_PRE_DEFAULT_S = 10

OPENING_MOTION_DATASET = "vivo_store_opening_motion_v1"


def _clip_pre_seconds(detection_type: str | None) -> int:
    return _CLIP_PRE_S.get(detection_type or "", _CLIP_PRE_DEFAULT_S)


def _frames_dir() -> Path:
    p = (Path(settings.recordings_dir) / "feedback_frames"
         / datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _grab_frame(clip_path: str, at_seconds: float, out_path: Path) -> bool:
    """Extract one frame at `at_seconds` into the clip. False when the
    clip is unreadable or the offset is past the end."""
    try:
        import cv2
    except ImportError:
        return False
    cap = cv2.VideoCapture(clip_path)
    try:
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_seconds * 1000.0))
        ok, frame = cap.read()
        if not ok or frame is None:
            return False
        return bool(cv2.imwrite(str(out_path), frame,
                                [cv2.IMWRITE_JPEG_QUALITY, 90]))
    finally:
        cap.release()


def _phase_for_offset(offset_from_event_s: float) -> str:
    """Coarse motion phase relative to the crossing moment (t=0)."""
    if offset_from_event_s < -15:
        return "approaching"
    if offset_from_event_s < -3:
        return "at_door"
    if offset_from_event_s <= 2:
        return "crossing"
    return "entering"


@celery_app.task(name="training.harvest_temporal_frames", ignore_result=True)
def harvest_temporal_frames(alert_id: int) -> None:
    """±1s context frames for one confirmed alert. Idempotent via the
    source_extra.harvest marker; silently skips when no clip exists
    (clips are extracted within minutes of the alert — feedback clicks
    normally come much later)."""
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent, TrainingImage
    from app.training.feedback_loop import _build_source_extra, _ensure_dataset

    with SessionLocal() as db:
        a = db.get(Alert, alert_id)
        ev = db.get(DetectionEvent, a.event_id) if a else None
        if not a or not ev:
            return
        clip = (ev.extra or {}).get("alert_clip_path")
        if not clip or not os.path.exists(clip):
            log.info("temporal harvest: alert %s has no clip — skipped",
                     alert_id)
            return
        already = (db.query(TrainingImage)
                     .filter(TrainingImage.source_alert_id == alert_id)
                     .all())
        if any((i.source_extra or {}).get("harvest") == "temporal_context"
               for i in already):
            return
        cls = ev.detection_type
        ds = _ensure_dataset(
            db, f"feedback-{cls}", [cls],
            description="auto: confirmed alerts (positive feedback pool)")
        pre = _clip_pre_seconds(cls)
        saved = 0
        for off in (-1.0, +1.0):
            out = _frames_dir() / f"alert{alert_id}_t{off:+.0f}s.jpg"
            if not _grab_frame(clip, pre + off, out):
                continue
            src = _build_source_extra(db, ev, a, "correct")
            src["harvest"] = "temporal_context"
            src["temporal_offset_s"] = off
            db.add(TrainingImage(
                dataset_id=ds.id, camera_id=ev.camera_id,
                file_path=str(out),
                labeled=False,           # → hourly pseudo-labeler annotates
                source_extra=src, source_alert_id=alert_id))
            saved += 1
        if saved:
            db.commit()
            log.info("temporal harvest: alert %s → %d context frames "
                     "(pseudo-label pending)", alert_id, saved)


@celery_app.task(name="training.run_shop_opening_specialist",
                 ignore_result=True)
def run_shop_opening_specialist(max_alerts: int = 200) -> None:
    """Build the opening-motion dataset from confirmed shop_open_close
    clips, then queue the 30-epoch specialist fine-tune."""
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent, TrainingImage, TrainingJob
    from app.training.feedback_loop import _build_source_extra, _ensure_dataset
    from app.training.orchestrator import _pick_parent_model

    with SessionLocal() as db:
        ds = _ensure_dataset(
            db, OPENING_MOTION_DATASET, ["person"],
            description="auto: store-opening motion sequences "
                        "(-30s..+5s, 10 frames per confirmed opening)")
        done_alerts = {
            i.source_alert_id
            for i in db.query(TrainingImage)
                       .filter(TrainingImage.dataset_id == ds.id).all()
            if i.source_alert_id}
        alerts = (db.query(Alert, DetectionEvent)
                    .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                    .filter(DetectionEvent.detection_type == "shop_open_close",
                            Alert.status == "confirmed")
                    .order_by(Alert.created_at.desc())
                    .limit(max_alerts).all())
        frames = 0
        for a, ev in alerts:
            if a.id in done_alerts:
                continue
            clip = (ev.extra or {}).get("alert_clip_path")
            if not clip or not os.path.exists(clip):
                continue
            pre = _clip_pre_seconds("shop_open_close")     # 45s before event
            # 10 samples spanning event-30s .. event+5s.
            offsets = [-30 + i * (35.0 / 9.0) for i in range(10)]
            for idx, off in enumerate(offsets):
                out = _frames_dir() / f"opening{a.id}_f{idx}.jpg"
                if not _grab_frame(clip, pre + off, out):
                    continue
                src = _build_source_extra(db, ev, a, "correct")
                src["harvest"] = "opening_motion"
                src["temporal_offset_s"] = round(off, 1)
                # Phase is curator metadata, NOT a YOLO class — there is
                # no ground truth to train phase classes on.
                src["frame_position"] = _phase_for_offset(off)
                db.add(TrainingImage(
                    dataset_id=ds.id, camera_id=ev.camera_id,
                    file_path=str(out), labeled=False,
                    source_extra=src, source_alert_id=a.id))
                frames += 1
        db.commit()
        total = (db.query(TrainingImage)
                   .filter(TrainingImage.dataset_id == ds.id).count())
        log.info("opening-motion dataset: +%d frames this run, %d total",
                 frames, total)
        if total < 30:
            log.warning("opening-motion: only %d frames — not queueing a "
                        "job yet (need 30+)", total)
            return
        parent = _pick_parent_model(db, "shop_open_close")
        cfg = {
            "incremental_finetune": parent is not None,
            "detection_type":       "shop_open_close",
            "resume_from_model_id": parent.id if parent else None,
            "epochs":               30,
            "batch":                16,
            "imgsz":                640,
            "lr0":                  0.0005,
            "augment":              True,
            # Lighting-heavy augmentation: openings happen at 07:00-09:00
            # EAT under shutters/backlight — push value/saturation jitter
            # well above the defaults.
            "augmentation":         {"hsv_v": 0.6, "hsv_s": 0.55,
                                     "hsv_h": 0.02},
            "origin":               "shop_opening_specialist",
        }
        job = TrainingJob(model_name="vivo_shop_opening_motion",
                          dataset_id=ds.id, config_json=cfg,
                          status="queued", priority=2, total_epochs=30)
        db.add(job); db.commit(); db.refresh(job)
        from app.tasks.training import run_training_job
        res = run_training_job.delay(job.id)
        job.celery_task_id = getattr(res, "id", None)
        db.commit()
        log.info("opening-motion specialist queued: job=%s dataset=%s "
                 "images=%d", job.id, ds.id, total)


@celery_app.task(name="training.run_store_specialist", ignore_result=True)
def run_store_specialist(store_name: str = "Vivo Yaya") -> None:
    """Per-store person/intrusion specialist: copy the store's feedback
    rows (positives keep annotations; negatives stay annotation-free)
    into vivo_{slug}_person_v1 and queue a 15-epoch fine-tune whose
    cfg.assign_store_id points the store's cameras at the result."""
    from app.database import SessionLocal
    from app.models import (Annotation, Camera, Dataset, Store,
                            TrainingImage, TrainingJob)
    from app.training.feedback_loop import _ensure_dataset
    from app.training.orchestrator import _pick_parent_model

    with SessionLocal() as db:
        store = (db.query(Store)
                   .filter(Store.name.ilike(store_name)).first())
        if store is None:
            log.error("store specialist: store %r not found", store_name)
            return
        cam_ids = [c.id for c in db.query(Camera)
                                    .filter(Camera.store_id == store.id).all()]
        if not cam_ids:
            log.error("store specialist: store %r has no cameras", store_name)
            return
        slug = "".join(ch for ch in store.name.lower().replace(" ", "_")
                       if ch.isalnum() or ch == "_")
        ds = _ensure_dataset(
            db, f"vivo_{slug}_person_v1", ["person", "intrusion"],
            description=f"auto: {store.name} person/intrusion specialist "
                        f"(camera-specific lighting + angles)")
        # Source pools: person + intrusion feedback, this store's cameras.
        pool_names = ["feedback-person", "feedback-intrusion",
                      "feedback-negative-person", "feedback-negative-intrusion"]
        pools = {d.name: d for d in db.query(Dataset)
                                       .filter(Dataset.name.in_(pool_names))}
        existing_paths = {
            i.file_path for i in db.query(TrainingImage)
                                    .filter(TrainingImage.dataset_id == ds.id)}
        copied_pos = copied_neg = 0
        for name, pool in pools.items():
            negative = name.startswith("feedback-negative-")
            rows = (db.query(TrainingImage)
                      .filter(TrainingImage.dataset_id == pool.id,
                              TrainingImage.camera_id.in_(cam_ids)).all())
            for src_img in rows:
                if not src_img.file_path or src_img.file_path in existing_paths:
                    continue
                if not os.path.exists(src_img.file_path):
                    continue
                clone = TrainingImage(
                    dataset_id=ds.id, camera_id=src_img.camera_id,
                    file_path=src_img.file_path, labeled=True,
                    source_extra={**(src_img.source_extra or {}),
                                  "harvest": "store_specialist",
                                  "source_pool": name},
                    source_alert_id=src_img.source_alert_id)
                db.add(clone); db.flush()
                existing_paths.add(src_img.file_path)
                if negative:
                    copied_neg += 1
                    continue                 # background sample: no boxes
                for ann in db.query(Annotation).filter(
                        Annotation.image_id == src_img.id).all():
                    db.add(Annotation(image_id=clone.id,
                                      class_label=ann.class_label,
                                      bbox_json=ann.bbox_json,
                                      verified=ann.verified))
                copied_pos += 1
        db.commit()
        pos_total = (db.query(TrainingImage)
                       .join(Annotation,
                             Annotation.image_id == TrainingImage.id)
                       .filter(TrainingImage.dataset_id == ds.id)
                       .distinct().count())
        log.info("store specialist %s: +%d pos +%d neg this run "
                 "(%d annotated total)", store.name, copied_pos,
                 copied_neg, pos_total)
        if pos_total < 15:
            log.warning("store specialist %s: only %d annotated images — "
                        "not queueing a job yet (need 15+)",
                        store.name, pos_total)
            return
        parent = _pick_parent_model(db, "intrusion")
        cfg = {
            "incremental_finetune": parent is not None,
            "detection_type":       "intrusion",
            "resume_from_model_id": parent.id if parent else None,
            "epochs":               15,
            "batch":                16,
            "imgsz":                640,
            "lr0":                  0.0005,
            "augment":              True,
            "origin":               "store_specialist",
            # Trainer post-step: point this store's cameras at the new
            # model once the model gate passes.
            "assign_store_id":      store.id,
        }
        job = TrainingJob(model_name=f"vivo_{slug}_person",
                          dataset_id=ds.id, config_json=cfg,
                          status="queued", priority=3, total_epochs=15)
        db.add(job); db.commit(); db.refresh(job)
        from app.tasks.training import run_training_job
        res = run_training_job.delay(job.id)
        job.celery_task_id = getattr(res, "id", None)
        db.commit()
        log.info("store specialist queued: store=%s job=%s dataset=%s",
                 store.name, job.id, ds.id)
