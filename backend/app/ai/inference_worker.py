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
from app.models import AIModel, Camera, DetectionConfig, DetectionEvent, Store, Zone, Alert
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

    # ------------------------------------------------------------------
    # Auto-enable detection types that the operator has implicitly opted
    # into via zone tagging. If a zone has detection_types_json=["queue"]
    # the QueueDetector should run for that camera even if the operator
    # never toggled "queue" in the AI Settings page. This eliminates the
    # most common "I configured it but nothing happens" failure mode.
    # ------------------------------------------------------------------
    zone_types: set[str] = set()
    for z in zones:
        for t in (z["detection_types_json"] or []):
            zone_types.add(t)
    for t in zone_types:
        if t not in cfg:
            cfg[t] = {
                "enabled": True,
                "confidence_threshold": 0.4,
                "min_object_size": 0,
                "detection_every_n_frames": 1,
                "dwell_time_seconds": None,
                "crowd_threshold": None,
                "extra": None,
                "schedule_json": None,
            }
        elif not cfg[t].get("enabled"):
            cfg[t] = {**cfg[t], "enabled": True}

    # ------------------------------------------------------------------
    # Always-on metric detectors. Rule 1 of the overhaul: every camera
    # contributes to these the moment it's attached — no toggles, no
    # zone tags, no setup. Each detector defends itself against missing
    # config inside evaluate() too, so this is belt-and-suspenders.
    # ------------------------------------------------------------------
    ALWAYS_ON = (
        "occupancy_metrics",   # headcount KPI
        "heatmap",             # footfall grid
        "unique_visitor",      # daily dedup'd visitors
        "customer_journey",    # zone-sequence path
        # 'demographic' removed — it requires a trained age/gender
        # model. Without one, every detection collapsed into the
        # 'unknown' bucket AND the dims-equality SQL it used crashed
        # the whole detector chain with InFailedSqlTransaction.
    )
    for t in ALWAYS_ON:
        cfg[t] = {
            **cfg.get(t, {}),
            "enabled": True,
            "confidence_threshold": cfg.get(t, {}).get("confidence_threshold", 0.4),
            "detection_every_n_frames": cfg.get(t, {}).get("detection_every_n_frames", 1),
        }

    weights = settings.default_model
    if cam.ai_model_id:
        m = db.get(AIModel, cam.ai_model_id)
        if m:
            weights = m.weights_path or settings.default_model
    return cam, zones, cfg, weights


# Detector types that emit DetectionEvents but should NOT create an
# Alert row. These are metric-style detectors — they show up on the
# dashboard's KPI tiles, not in the Alerts & Incidents feed. Keeping
# them silent stops the alert feed from drowning in routine signals.
# Mirrors app.api.detection_config.SKIP_ALERTS.
_SKIP_ALERT_TYPES: set[str] = {
    "heatmap", "customer_journey", "unique_visitor", "entry_exit",
    "demographic", "occupancy_metrics",
}


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
    # Operator-visible alert — only for detectors NOT in the silent
    # metric set. The DetectionEvent itself is still persisted so the
    # detector activity table and ML-feedback paths see every event.
    if ev.detection_type not in _SKIP_ALERT_TYPES:
        db.add(Alert(event_id=rec.id, status="new"))
        log.info("Created alert: %s for camera %s (event=%s)",
                 ev.detection_type, camera_id, rec.id)
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

            # Store metadata — cached per-frame so detectors don't each hit Postgres.
            store_id = cam.store_id
            business_hours = None
            store_tz = "UTC"
            if store_id is not None:
                store = db.get(Store, store_id)
                if store:
                    business_hours = store.business_hours_json
                    store_tz = store.timezone or "UTC"

            ctx = DetectorContext(
                camera_id=camera_id, timestamp=time.time(),
                raw_detections=raw, tracks=tracks, zones=zones, config=cfg,
                db=db, store_id=store_id,
                business_hours=business_hours, store_timezone=store_tz,
                frame_bgr=frame,
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
                    # Postgres aborts the whole transaction on the
                    # first error — subsequent metric/event writes
                    # would all fail with InFailedSqlTransaction.
                    # Roll back so the next detector starts clean.
                    try:
                        db.rollback()
                    except Exception:
                        pass

            try:
                db.commit()
            except Exception as e:
                # Should be rare now that per-detector rollbacks
                # happen, but the final commit can still hit a unique
                # constraint or a connection blip. Roll back and let
                # the loop move on — we'd rather skip a frame than
                # crash the worker for this camera.
                log.warning("camera %s: commit failed (%s) — rolling back",
                            camera_id, e)
                try:
                    db.rollback()
                except Exception:
                    pass
                # Skip the rest of this frame; the next iteration of
                # the while loop will refresh the session state.
                time.sleep(poll_interval)
                continue

            # Bust this store's analytics cache so the dashboard's
            # tile endpoints see fresh data on the next request. One
            # INCR per per-camera-loop iteration — cheap. We skip
            # this when there were no emitted events to keep the
            # cache hot for stores where nothing changed.
            if events_emitted and store_id is not None:
                try:
                    from app.utils.cache import bump_store_version
                    bump_store_version(store_id)
                except Exception:
                    pass

            # Publish alerts to the live UI feed. Same skip-list as
            # _persist_event — silent metric detectors never reach the
            # vg:pub:alerts channel so the operator feed stays focused
            # on actionable incidents.
            for ev in events_emitted:
                if ev.get("detection_type") in _SKIP_ALERT_TYPES:
                    continue
                try:
                    pub.publish("vg:pub:alerts", json.dumps(ev))
                except Exception:
                    pass

        time.sleep(poll_interval)
