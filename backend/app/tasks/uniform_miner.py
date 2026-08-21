"""Automatic staff-uniform crop miner (Part 6).

Every 2 hours, mine live camera frames for uniform training data so the
uniform_compliance detector stops being starved (the simulation agent is only
a QA probe — it never produced training crops). For cameras that have a
`staff_zone` or `counter` zone:

  - pull the latest cached frame (vg:frame:{cam_id}, raw JPEG),
  - run YOLO person detection,
  - for each person crop run uniform_features() HSV analysis:
      * dual_black AND centroid inside a staff_zone  -> POSITIVE
        (vivo_staff_uniform_v1, label "vivo_all_black"),
      * not black AND centroid NOT in any staff zone -> NEGATIVE
        (feedback-negative-uniform, label "civilian"),
      * anything else -> skip (ambiguous).

Each saved crop gets an orange-box review preview (write_preview). Per-camera
dedup via vg:sim:crop:{cam_id} (2h TTL); max 30 cameras per run rotating via
vg:sim:crop:cursor. Runs on the alerts worker (opencv present). Best-effort
throughout — a per-camera failure never aborts the run.

TrainingImage has no source/label/approved columns (those are on
TrainingSample, which the trainer doesn't read), so provenance is stored in
source_extra and positives carry an Annotation — exactly what
write_yolo_dataset_yaml consumes.
"""
from __future__ import annotations

import gc
import logging

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

POSITIVE_DATASET = "vivo_staff_uniform_v1"
NEGATIVE_DATASET = "feedback-negative-uniform"
POSITIVE_LABEL   = "vivo_all_black"
NEGATIVE_LABEL   = "civilian"

MAX_CAMERAS_PER_RUN = 30
CROP_DEDUP_TTL_SEC  = 2 * 3600
MIN_PERSON_CONF     = 0.35
_CURSOR_KEY = "vg:sim:crop:cursor"


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=False)


def _centroid(bbox_norm: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox_norm
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def run_uniform_mining() -> dict:
    """Core miner. Returns a summary dict. Never raises."""
    import numpy as np
    import cv2
    from app.database import SessionLocal
    from app.models import Zone, TrainingImage, Annotation
    from app.stream.frame_buffer import FrameBuffer
    from app.ai.yolov8_runner import infer
    from app.ai.detectors.base import COCO_PERSON
    from app.ai.detectors.uniform_compliance import uniform_features
    from app.ai.zone_logic import point_in_polygon
    from app.training.feedback_loop import _ensure_dataset
    from app.training.dataset import save_uploaded_image

    summary = {"cameras_processed": 0, "staff_crops": 0,
               "customer_crops": 0, "skipped": 0, "no_frame": 0}

    # Cameras that have a staff_zone or counter zone (only those can label
    # positives/negatives by geometry).
    with SessionLocal() as db:
        zrows = (db.query(Zone.camera_id, Zone.detection_types_json,
                          Zone.polygon_coords_json)
                   .all())
        staff_polys: dict[int, list] = {}     # cam_id -> [staff_zone polygons]
        eligible: set[int] = set()
        for cam_id, dtypes, poly in zrows:
            tags = set(dtypes or [])
            if {"staff_zone", "counter"} & tags:
                eligible.add(cam_id)
            if "staff_zone" in tags and poly:
                staff_polys.setdefault(cam_id, []).append(poly)
        cam_ids = sorted(c for c in eligible)

    if not cam_ids:
        log.info("uniform_miner: no cameras with staff_zone/counter zones")
        return summary

    r = _redis()
    try:
        cursor = int(r.get(_CURSOR_KEY) or 0)
    except Exception:
        cursor = 0
    cursor %= len(cam_ids)
    batch = [cam_ids[(cursor + i) % len(cam_ids)]
             for i in range(min(MAX_CAMERAS_PER_RUN, len(cam_ids)))]

    fb = FrameBuffer()
    pos_ds = neg_ds = None

    with SessionLocal() as db:
        for cid in batch:
            try:
                dkey = f"vg:sim:crop:{cid}".encode()
                if r.get(dkey):
                    continue                      # processed within 2h
                jpeg = fb.latest_jpeg(int(cid), prefer_overlay=False)
                if not jpeg:
                    summary["no_frame"] += 1
                    continue
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    summary["no_frame"] += 1
                    continue
                summary["cameras_processed"] += 1
                r.set(dkey, b"1", ex=CROP_DEDUP_TTL_SEC)   # mark early (idempotent)

                h, w = frame.shape[:2]
                persons = [d for d in infer(frame, conf=MIN_PERSON_CONF)
                           if d["cls"] in COCO_PERSON]
                polys = staff_polys.get(cid, [])
                for det in persons:
                    bbox = det["bbox_norm"]
                    feats = uniform_features(frame, bbox)
                    if feats is None:
                        summary["skipped"] += 1
                        continue
                    cx, cy = _centroid(bbox)
                    in_staff = any(point_in_polygon(cx, cy, p) for p in polys)
                    dual_black = bool(feats.get("dual_black"))

                    if dual_black and in_staff:
                        kind, label = "pos", POSITIVE_LABEL
                    elif (not dual_black) and (not in_staff):
                        kind, label = "neg", NEGATIVE_LABEL
                    else:
                        summary["skipped"] += 1
                        continue

                    # Crop the person (clean pixels — no overlay).
                    x1, y1, x2, y2 = det["bbox_px"]
                    x1i, y1i = max(0, int(x1)), max(0, int(y1))
                    x2i, y2i = min(w, int(x2)), min(h, int(y2))
                    if x2i - x1i < 8 or y2i - y1i < 16:
                        summary["skipped"] += 1
                        continue
                    crop = frame[y1i:y2i, x1i:x2i]
                    ok, buf = cv2.imencode(".jpg", crop)
                    if not ok:
                        summary["skipped"] += 1
                        continue

                    if kind == "pos":
                        if pos_ds is None:
                            pos_ds = _ensure_dataset(
                                db, POSITIVE_DATASET, [POSITIVE_LABEL],
                                description="auto: mined staff all-black uniform crops")
                        ds = pos_ds
                    else:
                        if neg_ds is None:
                            neg_ds = _ensure_dataset(
                                db, NEGATIVE_DATASET, [],
                                description="auto: mined civilian (non-uniform) crops")
                        ds = neg_ds

                    fname = f"mined_cam{cid}_{label}.jpg"
                    path = save_uploaded_image(ds.id, fname, bytes(buf))
                    img = TrainingImage(
                        dataset_id=ds.id, camera_id=int(cid),
                        file_path=str(path), labeled=True,
                        source_kind="auto_live_uniform_miner",
                        eligible_for_training=False,
                        review_state="pending",
                        source_extra={
                            "source": "auto_live_uniform_miner", "approved": False,
                            "verified": False, "label": label,
                            "dual_black": dual_black, "in_staff_zone": in_staff,
                            "uniform_confidence": round(float(feats.get("confidence") or 0), 3),
                        },
                    )
                    db.add(img); db.flush()
                    # Positive → full-frame annotation (the crop IS the person).
                    if kind == "pos":
                        db.add(Annotation(image_id=img.id, class_label=label,
                                          bbox_json=[0.5, 0.5, 1.0, 1.0],
                                          verified=False, auto_suggested=True))
                        summary["staff_crops"] += 1
                    else:
                        summary["customer_crops"] += 1
                    db.commit()

                    # Orange-box review preview (best-effort, worker has opencv).
                    try:
                        from app.training.image_preview import write_preview
                        prev = write_preview(str(path), label=label, bbox_norm=None)
                        if prev:
                            img.preview_path = prev
                            db.commit()
                    except Exception:
                        log.warning("uniform_miner: preview failed image=%s", img.id)
            except Exception as e:
                log.exception("uniform_miner: camera=%s failed: %s", cid, e)
                try: db.rollback()
                except Exception: pass
            finally:
                gc.collect()

    try:
        r.set(_CURSOR_KEY, str((cursor + len(batch)) % len(cam_ids)).encode())
    except Exception:
        pass

    log.info("uniform_miner: cameras=%d staff_crops=%d customer_crops=%d "
             "skipped=%d no_frame=%d (batch=%d of %d eligible)",
             summary["cameras_processed"], summary["staff_crops"],
             summary["customer_crops"], summary["skipped"],
             summary["no_frame"], len(batch), len(cam_ids))
    return summary


@celery_app.task(name="training.mine_live_uniform_crops", ignore_result=True)
def mine_uniform_crops() -> None:
    """Beat entry — runs the miner every 2 hours. Best-effort."""
    try:
        run_uniform_mining()
    except Exception as e:
        log.exception("uniform_miner: run failed: %s", e)
