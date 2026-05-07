"""AI inference worker.

Runs in the Celery `worker` service. For each active camera:
  - polls Redis for the latest JPEG (skips frames if it's stale)
  - decodes to ndarray
  - runs YOLOv8
  - feeds the IOU tracker
  - calls every relevant detector
  - persists DetectionEvent rows + creates Alert rows
  - publishes alerts on the `vg:pub:alerts` Redis channel for live UI

Designed to run as a long-lived task per camera, started/stopped by the
Celery beat scheduler (or directly by the API on camera CRUD).
"""
from __future__ import annotations
import io
import json
import logging
import time
from datetime import datetime, timezone

import numpy as np
import redis
from PIL import Image
from sqlalchemy.orm import Session

from app.ai.detectors import DetectorRegistry
from app.ai.detectors.base import DetectorContext
from app.ai.tracker import IOUTracker
from app.ai.yolov8_runner import infer
from app.config import settings
from app.database import SessionLocal
from app.models import AIModel, Camera, DetectionConfig, DetectionEvent, Zone, Alert
from app.stream.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)


def _load_camera_state(db: Session, camera_id: int) -> tuple[Camera | None, list[dict], dict, str]:
    cam = db.get(Camera, camera_id)
    if not cam or not cam.ai_enabled:
        return None, [], {}, settings.default_model

    zones: list[dict] = []
    for z in db.query(Zone).filter(Zone.camera_id == camera_id):
        zones.append({
            "id": z.id, "name": z.name, "shape": z.shape,
            "polygon_coords_json": z.polygon_coords_json,
            "detection_types_json": z.detection_types_json,
            "active_schedule_json": z.active_schedule_json,
            "suppressed": z.suppressed,
        })

    cfg: dict = {}
    for c in db.query(DetectionConfig).filter(DetectionConfig.camera_id == camera_id):
        cfg[c.detection_type] = {
            "enabled": c.enabled,
            "confidence_threshold": c.confidence_threshold,
            "min_object_size": c.min_object_size,
            "detection_every_n_frames": c.detection_every_n_frames,
            "dwell_time_seconds": c.dwell_time_seconds,
            "crowd_threshold": c.crowd_threshold,
            "extra": c.extra,
            "schedule_json": c.schedule_json,
        }

    weights = settings.default_model
    if cam.ai_model_id:
        m = db.get(AIModel, cam.ai_model_id)
        if m:
            weights = m.weights_path or settings.default_model
    return cam, zones, cfg, weights


def _persist_event(db: Session, camera_id: int, ev, model_id: int | None) -> int:
    rec = DetectionEvent(
        camera_id=camera_id,
        zone_id=ev.zone_id,
        detection_type=ev.detection_type,
        confidence=ev.confidence,
        bbox_json=ev.bbox_norm,
        extra=ev.extra or None,
        model_id=model_id,
    )
    db.add(rec)
    db.flush()
    db.add(Alert(event_id=rec.id, status="new"))
    return rec.id


def run_for_camera(camera_id: int, *, max_seconds: int = 0,
                   poll_interval: float = 0.1) -> None:
    """Inference loop. `max_seconds=0` means run forever."""
    registry = DetectorRegistry()
    tracker  = IOUTracker()
    buffer   = FrameBuffer()
    pub      = redis.from_url(settings.redis_url)
    started  = time.time()
    last_seen_ts: float = 0.0
    frame_idx = 0

    while True:
        if max_seconds and time.time() - started >= max_seconds:
            return

        with SessionLocal() as db:
            cam, zones, cfg, weights = _load_camera_state(db, camera_id)
            if not cam:
                log.info("camera %s gone or disabled", camera_id)
                return

            jpeg = buffer.latest_jpeg(camera_id)
            if not jpeg:
                time.sleep(poll_interval)
                continue
            health = buffer.health(camera_id) or {}
            ts = float(health.get("last_frame_at") or 0.0)
            if ts <= last_seen_ts:
                # Same frame as last loop — skip.
                time.sleep(poll_interval)
                continue
            last_seen_ts = ts
            frame_idx += 1

            try:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            except Exception as e:
                log.warning("camera %s: bad jpeg (%s)", camera_id, e)
                time.sleep(poll_interval)
                continue
            frame = np.array(img)[:, :, ::-1]   # RGB → BGR for OpenCV/YOLO

            try:
                raw = infer(frame, weights=weights, conf=0.25)
            except Exception as e:
                log.exception("camera %s: inference failed: %s", camera_id, e)
                time.sleep(1.0)
                continue

            tracks = tracker.update(raw)
            ctx = DetectorContext(
                camera_id=camera_id, timestamp=time.time(),
                raw_detections=raw, tracks=tracks, zones=zones, config=cfg,
            )

            events_emitted: list[dict] = []
            for det in registry.detectors_for(camera_id):
                # Per-frame skip honouring detection_every_n_frames.
                step = int((cfg.get(det.detection_type) or {}).get("detection_every_n_frames", 1) or 1)
                if frame_idx % max(1, step) != 0:
                    continue
                try:
                    for ev in det.evaluate(ctx):
                        eid = _persist_event(db, camera_id, ev, cam.ai_model_id)
                        events_emitted.append({
                            "id": eid,
                            "camera_id": camera_id,
                            "detection_type": ev.detection_type,
                            "confidence": ev.confidence,
                            "bbox_norm": ev.bbox_norm,
                            "zone_id": ev.zone_id,
                            "track_id": ev.track_id,
                            "extra": ev.extra,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        })
                except Exception as e:
                    log.exception("detector %s failed: %s", det.detection_type, e)

            db.commit()

            # Publish alerts to the live UI feed.
            for ev in events_emitted:
                try:
                    pub.publish("vg:pub:alerts", json.dumps(ev))
                except Exception:
                    pass

        time.sleep(poll_interval)
