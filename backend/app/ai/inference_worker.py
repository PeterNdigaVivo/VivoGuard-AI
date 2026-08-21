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
import os
import time
from datetime import datetime, timezone

import numpy as np
import redis
from PIL import Image
from sqlalchemy.orm import Session

from app.ai.detectors import DetectorRegistry
from app.ai.detectors.base import COCO_PERSON, DetectorContext
from app.ai.snapshot import SNAPSHOT_TYPES
from app.ai.yolov8_runner import infer, load_model, resolve_weights
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

    # Weights resolution, in priority order:
    #   1. the camera's explicitly-assigned model (cam.ai_model_id), if its
    #      file is on disk;
    #   2. otherwise the latest chain-wide DEPLOYED model
    #      (ai_models.deployed=true), if its file is on disk — so a trained
    #      model that's been deployed is used even for cameras that were never
    #      individually re-pointed at it;
    #   3. otherwise the base model (settings.default_model, yolov8n.pt).
    weights: str | None = None
    if cam.ai_model_id:
        m = db.get(AIModel, cam.ai_model_id)
        if m and m.weights_path and os.path.exists(m.weights_path):
            weights = m.weights_path
    # The chain-wide deployed-model fallback is OFF by default: the deployed
    # retail model (v25) is a specialist whose classes are NOT COCO "person",
    # so routing the GENERAL inference pass through it makes every person-based
    # detector see 0 persons (chain-wide outage). Only used when explicitly
    # enabled AND the deployed model is a COCO-person detector.
    if weights is None and getattr(settings, "use_deployed_model_for_inference", False):
        weights = _deployed_weights(db)
    if weights is None:
        weights = settings.default_model
    return cam, zones, cfg, weights


def _deployed_weights(db: Session) -> str | None:
    """Latest chain-wide deployed model's weights, when the file exists on
    disk. Returns None so callers fall back to the base model."""
    m = (db.query(AIModel)
           .filter(AIModel.deployed.is_(True))
           .order_by(AIModel.id.desc())
           .first())
    if m and m.weights_path and os.path.exists(m.weights_path):
        return m.weights_path
    return None


# Detector types that emit DetectionEvents but should NOT create an
# Alert row. These are metric-style detectors — they show up on the
# dashboard's KPI tiles, not in the Alerts & Incidents feed. Keeping
# them silent stops the alert feed from drowning in routine signals.
# Mirrors app.api.detection_config.SKIP_ALERTS.
_SKIP_ALERT_TYPES: set[str] = {
    "heatmap", "customer_journey", "unique_visitor", "entry_exit",
    "demographic", "occupancy_metrics",
    # checkout_dwell `checkout_complete` events are metric-only (every
    # session ≥30s emits one). The operator-facing long-session
    # ATTENTION alert is fired separately + gated at 8 min by
    # alerting.checkout_long_session_check. Without this skip, every
    # completed checkout created an ungated alert (the 41s / 148s
    # false alerts). DetectionEvent + metric_snapshots writes are
    # unaffected — the skip only suppresses Alert-row creation.
    "checkout_dwell",
}

# Temporal alert gate (FP-reduction Phase 3): a tracked object must persist
# for >= settings.temporal_gate_min_frames consecutive frames before its
# ALERT fires — the DetectionEvent row is ALWAYS written, only the Alert is
# held back, so metrics and the training loop see every detection. Exempt:
# the fast-fire store-open path (entry_exit / shop_open_close were tuned to
# min=1 after real missed-crossing pain) and every detector with its own
# bespoke P5 frame gate — double-gating them would change tuned behaviour.
_TEMPORAL_GATE_EXEMPT: set[str] = {
    "entry_exit", "shop_open_close",
    "checkout_dwell", "trespass", "uniform_compliance", "staff_present",
}

# Static person-shaped fixtures are suppressed only for alert types where a
# stationary false person can create an incident. Staff/compliance/analytics
# detectors still need stationary real people (for example a cashier at a
# counter), especially immediately after an inference-worker restart.
_STATIC_PERSON_FILTER_TYPES: set[str] = {
    "person", "intrusion", "trespass", "loitering",
}


def _persist_event(db: Session, camera_id: int, ev, model_id: int | None,
                   *, frame_bgr=None, store_name: str | None = None,
                   camera_name: str | None = None, store_id: int | None = None,
                   suppress_alert: bool = False) -> tuple[int, int | None]:
    """Always writes a DetectionEvent row. `suppress_alert` ONLY
    controls whether the companion Alert row is created — it must
    NEVER gate the underlying detection_event persistence (Vivo
    regression Jun 2026 traced to a path where a suppress-check
    exception silently dropped every person event for the frame)."""
    # Capture an annotated snapshot for the alert-worthy detector types
    # so the Alerts page can show the picture, not just the label.
    thumb_path = None
    if ev.detection_type in SNAPSHOT_TYPES and frame_bgr is not None and not suppress_alert:
        from app.ai.snapshot import capture_alert_snapshot
        # Keep the stamped caption in sync with the card title —
        # before-hours intrusion reads "Before", not "After".
        _cap = None
        if (ev.detection_type == "intrusion"
                and (ev.extra or {}).get("time_context") == "before_hours"):
            _cap = "Someone in Store Before Hours"
        thumb_path = capture_alert_snapshot(
            frame_bgr, ev.bbox_norm, ev.detection_type, camera_id,
            store_name=store_name, camera_name=camera_name,
            boxes=(ev.extra or {}).get("boxes"), title_override=_cap)
    # The detector dataclass carries `cls` (e.g. "shop_opened",
    # "crossing_in") but detection_events has no cls column — merge it
    # into extra so the sub-type survives persistence instead of being
    # silently dropped (the shop_open_close NULL-cls bug, Jul 2026).
    extra = dict(ev.extra or {})
    _cls = getattr(ev, "cls", None)
    if _cls and "cls" not in extra:
        extra["cls"] = _cls
    rec = DetectionEvent(
        camera_id=camera_id,
        zone_id=ev.zone_id,
        detection_type=ev.detection_type,
        confidence=ev.confidence,
        bbox_json=ev.bbox_norm,
        extra=extra or None,
        model_id=model_id,
        thumbnail_path=thumb_path,
    )
    db.add(rec)
    db.flush()
    # Operator-visible alert — only for detectors NOT in the silent
    # metric set, and not explicitly suppressed by the caller. The
    # DetectionEvent itself is still persisted so the detector activity
    # table and ML-feedback paths see every event.
    alert_id: int | None = None
    if ev.detection_type not in _SKIP_ALERT_TYPES and not suppress_alert:
        alert = Alert(event_id=rec.id, status="new")
        db.add(alert)
        db.flush()
        # Quality quarantine keeps the operator-facing record, snapshot and
        # clip, while marking it review-only before any live notification is
        # published. This is policy plumbing, not detector logic.
        from app.services.alert_quality import apply_quality_control
        apply_quality_control(db, alert, rec)
        # Shadow incident/evidence/outbox projection. A savepoint makes this
        # fail-open in the precise transactional sense: any projection error
        # rolls back only additive rows and leaves the baseline event+alert
        # intact. Both feature switches default off.
        if bool(getattr(settings, "incident_foundations_enabled", False)):
            try:
                with db.begin_nested():
                    from app.services.incident_foundations import (
                        record_alert_foundations,
                    )
                    record_alert_foundations(
                        db, alert, rec, store_id=store_id,
                        queue_delivery=bool(getattr(
                            settings, "delivery_outbox_enabled", False)),
                    )
            except Exception as e:
                log.warning(
                    "incident foundations failed alert=%s event=%s: %s; "
                    "baseline alert preserved", alert.id, rec.id, e,
                )
        alert_id = alert.id
        log.info("Created alert: %s for camera %s (event=%s)",
                 ev.detection_type, camera_id, rec.id)
        # Business-hours filmstrip — 6 smartly-timed snapshots bracketing
        # the incident (-10s, 0, +30/60/90/120s). Fire-and-forget on the
        # alerts queue; the task itself gates on type + the 09:00–20:00 EAT
        # window. .delay() is microseconds and must never block inference.
        try:
            from app.tasks.alert_snapshots import (
                schedule_alert_filmstrip, FILMSTRIP_TYPES,
            )
            if store_id is not None and ev.detection_type in FILMSTRIP_TYPES:
                schedule_alert_filmstrip.delay(
                    alert.id, camera_id, store_id, ev.detection_type,
                    datetime.now(timezone.utc).timestamp())
        except Exception as e:
            log.warning("filmstrip enqueue failed cam=%s: %s", camera_id, e)
        # VLM scene analysis — fire-and-forget on the alerts queue so
        # the 10s cloud call never blocks this inference loop. Guarded
        # by detection_type + a stored thumbnail; the task itself
        # re-checks vlm_enabled. .delay() is microseconds here.
        try:
            if (getattr(settings, "vlm_enabled", False)
                    and thumb_path
                    and ev.detection_type in set(getattr(settings, "vlm_alert_types", []) or [])):
                from app.tasks.vlm_tasks import analyse_alert_scene
                analyse_alert_scene.delay(alert.id)
        except Exception:
            pass
    return rec.id, alert_id


# Zone tags that make a bare "person" detection alert-worthy even
# during business hours — these are areas people shouldn't be in.
_PERSON_RESTRICTED_TAGS = {"restricted", "intrusion", "trespass", "high_value", "stockroom"}


def _person_alert_warranted(ev, zones: list[dict], store) -> bool:
    """A plain person detection only deserves an alert when it's
    genuinely notable: after hours (with the configurable grace
    window around opening/closing), OR inside a restricted/staff-only
    zone. During business hours, or inside the grace windows, it's
    just normal staff/customer traffic — we persist the metric but
    skip the alert.

    Grace windows (settings):
      person_afterhours_grace_before_min  default 60
      person_afterhours_grace_after_min   default 60
    so a staff member arriving at 08:50 EAT for a 09:00 opening does
    NOT trip URGENT intrusion, but 02:00 EAT still does.

    All time math runs in Africa/Nairobi via is_after_hours_with_grace().
    """
    # Restricted-zone check — find the event's zone among the list.
    if ev.zone_id is not None:
        for z in zones:
            if z.get("id") == ev.zone_id:
                if _PERSON_RESTRICTED_TAGS & set(z.get("detection_types_json") or []):
                    return True
                break
    if store is None:
        return False   # no store context → treat as a normal customer
    try:
        from app.utils.business_hours import is_after_hours_with_grace
        return is_after_hours_with_grace(
            store,
            grace_before_open_min=settings.person_afterhours_grace_before_min,
            grace_after_close_min=settings.person_afterhours_grace_after_min,
        )
    except Exception:
        return False


def _person_alert_recently_fired(camera_id: int) -> bool:
    """Per-camera Redis dedupe gate. Returns True when an after-hours
    person alert has already fired for `camera_id` within the last
    person_afterhours_dedupe_min minutes. Fails OPEN (allows the
    alert) on any Redis error so a broken cache never silences a
    genuine intrusion."""
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return bool(r.get(f"vg:person_afterhours:fired:{int(camera_id)}"))
    except Exception:
        return False


def _mark_person_alert_fired(camera_id: int) -> None:
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        ttl = max(60, int(settings.person_afterhours_dedupe_min) * 60)
        r.set(f"vg:person_afterhours:fired:{int(camera_id)}", "1", ex=ttl)
    except Exception:
        pass


# Reset the tracker when frames resume after a gap this long — ByteTrack IDs
# are meaningless across a stream outage.
TRACKER_RESET_GAP_S = 30.0


def _make_tracker(camera_id: int):
    """ByteTrack (via VivoGuardTracker) when supervision is importable;
    IOUTracker fallback otherwise. Both expose the same
    update(raw) -> list[(Track, det)] contract, so the loop and every
    detector are agnostic to which one is running."""
    try:
        from app.ai.tracker import VivoGuardTracker
        t = VivoGuardTracker(camera_id)
        log.info("camera %s: tracking via ByteTrack (supervision)", camera_id)
        return t
    except Exception as e:                                   # noqa: BLE001
        from app.ai.tracker import IOUTracker
        log.warning("camera %s: ByteTrack unavailable (%s) — IOUTracker fallback",
                    camera_id, e)
        return IOUTracker()


def run_for_camera(camera_id: int, *, max_seconds: int = 0,
                   poll_interval: float = 0.1) -> None:
    """Inference loop. `max_seconds=0` means run forever."""
    registry = DetectorRegistry()
    tracker  = _make_tracker(camera_id)
    buffer   = FrameBuffer()
    pub      = redis.from_url(settings.redis_url)
    started  = time.time()
    dets_pub_at = 0.0        # vg:dets publish throttle (1s cadence)
    recent_people: dict[int, float] = {}  # short occlusion/missed-frame hold

    # Sprint 2.2 cross-camera Re-ID. Encoder + gallery are lazily/best-
    # effort built; both stay None and the whole feature no-ops when
    # REID_ENABLED is false or torchvision is missing.
    reid_encoder = None
    reid_xtracker = None
    if settings.reid_enabled:
        try:
            from app.ai.reid_encoder import get_reid_encoder
            from app.ai.reid_tracker import CrossCameraTracker
            reid_encoder = get_reid_encoder()
            reid_xtracker = CrossCameraTracker(pub)
        except Exception as _reid_init_exc:             # noqa: BLE001
            log.warning("Re-ID init failed cam=%s (disabled): %s",
                        camera_id, _reid_init_exc)
            reid_encoder = reid_xtracker = None
    last_seen_ts: float = 0.0
    last_evict_ts: float = 0.0
    model_validated = False
    frame_idx = 0

    # Sprint 1.2 latency telemetry. Backend/format from HardwareEnv so
    # each perf row records which compute path produced the numbers.
    from app.ai.perf_tracker import PerfTracker
    from app.ai.env_config import HardwareEnv
    _env = HardwareEnv.detect()
    perf = PerfTracker(camera_id, backend=_env.backend,
                       fmt=_env.export_format, redis_client=pub)

    # try/finally guarantees emit_final() flushes the tail window on
    # BOTH exit paths (max_seconds reached AND camera gone / no frame).
    try:
      while True:
        if max_seconds and time.time() - started >= max_seconds:
            break

        with SessionLocal() as db:
            cam, zones, cfg, weights = _load_camera_state(db, camera_id)
            if not cam:
                log.info("camera %s gone or disabled", camera_id)
                break

            # Detect on pristine pixels — never read the overlay frame
            # the QueueDetector writes back, or YOLO would re-detect on
            # its own drawn boxes / labels.
            jpeg = buffer.latest_jpeg(camera_id, prefer_overlay=False)
            if not jpeg:
                time.sleep(poll_interval)
                continue
            health = buffer.health(camera_id) or {}
            ts = float(health.get("last_frame_at") or 0.0)
            if ts <= last_seen_ts:
                # Same frame as last loop — skip.
                time.sleep(poll_interval)
                continue
            # Stream resumed after a long outage — reset the tracker so stale
            # ByteTrack IDs don't bleed across the gap. (IOUTracker fallback
            # has no reset(); guarded.)
            gap = ts - last_seen_ts if last_seen_ts else 0.0
            if gap > TRACKER_RESET_GAP_S and hasattr(tracker, "reset"):
                tracker.reset()
                log.info("camera %s: tracker reset after %.0fs frame gap",
                         camera_id, gap)
            last_seen_ts = ts
            frame_idx += 1
            _t_iter = time.perf_counter()        # total-stage start

            _t0 = time.perf_counter()
            try:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            except Exception as e:
                log.warning("camera %s: bad jpeg (%s)", camera_id, e)
                time.sleep(poll_interval)
                continue
            frame = np.array(img)[:, :, ::-1]   # RGB → BGR for OpenCV/YOLO
            perf.record("frame_read", (time.perf_counter() - _t0) * 1000.0)

            # Startup validation — log the actual model + its person class ids
            # ONCE per camera so an outage cause (a model with no person class)
            # is obvious in the worker log immediately.
            if not model_validated:
                try:
                    _m = load_model(resolve_weights(weights))
                    _pids = getattr(_m, "_person_cls_ids", None)
                    log.info("Model validation cam=%s: weights=%s person_class_ids=%s%s",
                             camera_id, weights, _pids,
                             "  ← NO PERSON CLASS — person detectors will see 0!"
                             if _pids == [] else "")
                except Exception as _mv:
                    log.warning("Model validation failed cam=%s: %s", camera_id, _mv)
                model_validated = True

            _t0 = time.perf_counter()
            try:
                raw = infer(frame, weights=weights, conf=0.25)
            except Exception as e:
                log.exception("camera %s: inference failed: %s", camera_id, e)
                time.sleep(1.0)
                continue
            perf.record("inference", (time.perf_counter() - _t0) * 1000.0)

            _t_post = time.perf_counter()       # postprocess-stage start
            tracks = tracker.update(raw)
            # Evict lost tracks' confidence buffers every ~30s (ByteTrack
            # path only; IOUTracker has no evict_lost — guarded).
            if hasattr(tracker, "evict_lost") and (time.time() - last_evict_ts) > 30.0:
                tracker.evict_lost({tr.track_id for tr, _ in tracks})
                last_evict_ts = time.time()

            # Store metadata — cached per-frame so detectors don't each hit Postgres.
            store_id = cam.store_id
            business_hours = None
            store_tz = "Africa/Nairobi"
            store_name = None
            store = None
            if store_id is not None:
                store = db.get(Store, store_id)
                if store:
                    from app.utils.business_hours import normalise_business_hours
                    business_hours = normalise_business_hours(store.business_hours_json)
                    store_tz = store.timezone or "Africa/Nairobi"
                    store_name = store.name

            # --- Sprint 2.2: cross-camera Re-ID (best-effort, OFF the hot
            # path) ----------------------------------------------------
            # Encode 1-of-N frames per person track into a 512-d
            # appearance vector, match against the store-wide gallery, and
            # stash the resulting global_person_id on the Track.extra dict.
            # UniqueVisitorDetector reads it back and writes it onto the
            # VisitorTrack row, so unique-visitor counting + journeys can
            # de-duplicate the same shopper across cameras. Track.extra
            # persists frame-to-frame (IOUTracker reuses Track objects),
            # which is what makes the every-Nth-frame skip safe.
            if reid_encoder is not None and reid_xtracker is not None and store_id is not None:
                try:
                    _H, _W = frame.shape[:2]
                    _every = max(1, int(settings.reid_encode_every_n_frames))
                    for _tr, _det in tracks:
                        if _tr.cls not in COCO_PERSON:
                            continue
                        _n = int(_tr.extra.get("_reid_n", 0)) + 1
                        _tr.extra["_reid_n"] = _n
                        # Encode on the 1st sighting then every Nth after,
                        # so a brief track still gets one embedding. Using
                        # (n-1) % every == 0 keeps the 1st-frame encode and
                        # still works when every == 1 (encode every frame).
                        if (_n - 1) % _every != 0:
                            continue
                        _bb = _tr.bbox_norm or []
                        if len(_bb) != 4:
                            continue
                        _x1 = max(0, int(_bb[0] * _W)); _y1 = max(0, int(_bb[1] * _H))
                        _x2 = min(_W, int(_bb[2] * _W)); _y2 = min(_H, int(_bb[3] * _H))
                        # Skip degenerate / tiny crops — too small to ID.
                        if _x2 - _x1 < 16 or _y2 - _y1 < 32:
                            continue
                        _emb = reid_encoder.encode(frame[_y1:_y2, _x1:_x2])
                        if _emb is None:
                            continue
                        _gid = reid_xtracker.match_or_register(
                            _emb, store_id, camera_id, bbox=[_x1, _y1, _x2, _y2])
                        if _gid:
                            _tr.extra["global_person_id"] = _gid
                except Exception as _reid_exc:           # noqa: BLE001
                    log.debug("reid loop skipped cam=%s: %s", camera_id, _reid_exc)

            # Keep fixed false-positive boxes (chairs, mannequins and
            # merchandise mislabelled as people) out of actionable detector
            # input.  Previously we calculated this only for the live-count
            # widget, so those same static boxes still generated alerts.
            from app.ai.tracker import partition_static_person_tracks
            _filter_on = bool(getattr(settings, "mannequin_filter_enabled", True))
            _win = int(getattr(settings, "activity_static_window_frames", 10))
            _minpx = float(getattr(settings, "activity_static_displacement_px", 5))
            _fh, _fw = frame.shape[:2]
            detector_raw, detector_tracks, _static_ids = partition_static_person_tracks(
                raw, tracks, (_fw, _fh), enabled=_filter_on,
                min_px=_minpx, window=_win,
            )

            ctx = DetectorContext(
                camera_id=camera_id, timestamp=time.time(),
                raw_detections=raw, tracks=tracks,
                zones=zones, config=cfg,
                db=db, store_id=store_id,
                business_hours=business_hours, store_timezone=store_tz,
                frame_bgr=frame,
            )
            static_filtered_ctx = DetectorContext(
                camera_id=camera_id, timestamp=time.time(),
                raw_detections=detector_raw, tracks=detector_tracks,
                zones=zones, config=cfg,
                db=db, store_id=store_id,
                business_hours=business_hours, store_timezone=store_tz,
                frame_bgr=frame,
            )

            # track_id -> consecutive frames seen: ByteTrack's rolling
            # confidence buffer when available, history length under the
            # IOUTracker fallback. Drives the temporal alert gate below.
            frames_seen: dict[int, int] = {
                tr.track_id: int(tr.extra.get("frames_seen") or len(tr.history))
                for tr, _d in tracks
            }

            events_emitted: list[dict] = []
            # Per-frame dedupe set: kills back-to-back duplicate
            # detections (NMS misses, near-identical bboxes, concurrent-
            # worker overlap before the lock catches up). Signature is
            # (detection_type, cls, bbox rounded to 0.02) so we don't
            # drop legitimately co-present people on opposite ends of
            # the frame.
            seen_this_frame: set[tuple] = set()
            # One-line per-camera diagnostic for the entry_exit
            # detector specifically — operator can grep
            #     `entry_exit detector: camera=`
            # in the worker log to see whether a camera even reaches
            # the detector. Throttled by frame_idx so we log roughly
            # once per ~30 s of running at 5 FPS instead of every
            # frame.
            ee_zone_count = sum(
                1 for z in zones
                if "entry_exit" in (z.get("detection_types_json") or []))
            if ee_zone_count and frame_idx % 150 == 0:
                person_count = sum(1 for d in raw if d.get("cls") == "person")
                log.info("entry_exit detector: camera=%s zones=%d detections=%d",
                         camera_id, ee_zone_count, person_count)

            # Live Activity heartbeat — written every frame so the
            # /cameras/activity/live endpoint can rank cameras
            # purely from Redis with zero DB reads in the hot path.
            # Score is "people seen RIGHT NOW" (raw person count
            # in this frame); 5-min TTL so a camera that stops
            # streaming drops off naturally instead of lingering
            # at its last score.
            try:
                # Static-object (mannequin) filter: a real person's track
                # moves; a mannequin's bbox sits still frame after frame.
                # Judged from the tracker's own in-process Track.history
                # (last N bbox positions — no extra Redis state needed).
                # Tracks younger than the window count as MOVING, so a
                # person who just walked in is never suppressed. If
                # tracking is degraded (no ids at all), fall back to the
                # raw count rather than reporting zero.
                _persons = [d for d in raw if d.get("cls") == "person"]
                _static_flags = [d.get("track_id") in _static_ids for d in _persons]
                _moving = [d for d in _persons if d.get("track_id") not in _static_ids]
                _static_n = len(_persons) - len(_moving)
                from app.ai.tracker import update_recent_person_tracks
                _people_now, _held_n = update_recent_person_tracks(
                    recent_people, _moving, time.time(),
                    hold_seconds=float(getattr(
                        settings, "activity_track_hold_seconds", 5.0)),
                )
                # ByteTrack context (ADDITIVE for the tab endpoint):
                # MOVING persons only — the sentinel's snapshots and the
                # tab's counts both exclude static mannequins/fixtures.
                _tracked = [d for d in _moving
                            if d.get("track_id") is not None
                            and d.get("bbox_px")][:20]
                pub.set(
                    f"vg:activity:{camera_id}",
                    json.dumps({
                        "camera_id": camera_id,
                        "people":    _people_now,
                        "score":     float(_people_now),
                        "ts":        time.time(),
                        "tracker_ids": [int(d["track_id"]) for d in _tracked],
                        "bboxes_px": [[int(v) for v in d["bbox_px"]]
                                      for d in _tracked],
                        "static_filtered": _static_n,
                        "currently_visible": len(_moving),
                        "temporarily_occluded": _held_n,
                    }),
                    ex=300,
                )
                # Live overlay feed for the frontend (vg:dets:{cam}, 15s
                # TTL): EVERY person detection incl. static ones, with the
                # mannequin flag, so the UI can draw green boxes for people
                # and dashed-gray for filtered fixtures. Normalized corners
                # -> trivial %-based rendering at any container size.
                # THROTTLED to 1/s (Feature-2 spec) — the frontend polls at
                # 1 Hz, so per-frame writes above that were pure Redis load.
                if time.time() - dets_pub_at >= 1.0:
                    dets_pub_at = time.time()
                    pub.set(
                        f"vg:dets:{camera_id}",
                        json.dumps([
                            {"id": d.get("track_id"),
                             "cls": "person",
                             "conf": round(float(d.get("conf") or 0.0), 3),
                             "bbox": [round(float(v), 4)
                                      for v in (d.get("bbox_norm") or [0, 0, 0, 0])],
                             "is_static": _static_flags[i]}
                            for i, d in enumerate(_persons[:25])
                        ]),
                        ex=15,
                    )
            except Exception:
                pass

            for det in registry.detectors_for(camera_id):
                # Per-frame skip honouring detection_every_n_frames.
                step = int((cfg.get(det.detection_type) or {}).get("detection_every_n_frames", 1) or 1)
                if frame_idx % max(1, step) != 0:
                    continue
                try:
                    eval_ctx = (static_filtered_ctx
                                if det.detection_type in _STATIC_PERSON_FILTER_TYPES
                                else ctx)
                    for ev in det.evaluate(eval_ctx):
                        sig = (
                            ev.detection_type, getattr(ev, "cls", None),
                            tuple(round(float(c), 2) for c in (ev.bbox_norm or [])),
                        )
                        if sig in seen_this_frame:
                            continue
                        seen_this_frame.add(sig)
                        # Decide whether to suppress the ALERT for this
                        # event. Suppress=True means the DetectionEvent
                        # row IS STILL WRITTEN; only the Alert row is
                        # skipped. Wrapped in its own try block so a
                        # Redis blip / settings hiccup in the suppress
                        # check can never bubble up to the outer detector
                        # handler — which would rollback the whole
                        # transaction and drop every event queued for
                        # this frame (Vivo regression, Jun 2026).
                        suppress = False
                        if ev.detection_type == "person":
                            try:
                                if not _person_alert_warranted(ev, zones, store):
                                    suppress = True
                                elif _person_alert_recently_fired(camera_id):
                                    suppress = True
                                else:
                                    _mark_person_alert_fired(camera_id)
                            except Exception as _sup_exc:
                                # Fail safe: persist the event, skip the
                                # after-hours alert. Better to miss a
                                # tag than to silently lose the row.
                                log.warning(
                                    "person suppress-check raised cam=%s: %s "
                                    "— persisting event without alert",
                                    camera_id, _sup_exc)
                                suppress = True
                        # Temporal gate — suppress the ALERT (never the
                        # event row) when the underlying track hasn't
                        # persisted long enough; kills 1-frame blips.
                        if (not suppress
                                and ev.detection_type not in _SKIP_ALERT_TYPES
                                and ev.detection_type not in _TEMPORAL_GATE_EXEMPT
                                and ev.track_id is not None):
                            _need = int(getattr(settings,
                                                "temporal_gate_min_frames", 10))
                            _fs = frames_seen.get(ev.track_id, 0)
                            if _need > 1 and _fs < _need:
                                suppress = True
                                log.debug(
                                    "temporal gate: %s cam=%s track=%s "
                                    "frames_seen=%d < %d — alert suppressed",
                                    ev.detection_type, camera_id,
                                    ev.track_id, _fs, _need)
                        eid, alert_id = _persist_event(
                                             db, camera_id, ev, cam.ai_model_id,
                                             frame_bgr=frame, store_name=store_name,
                                             camera_name=cam.name, store_id=store_id,
                                             suppress_alert=suppress)
                        if suppress or alert_id is None:
                            continue  # not an operator-facing event
                        events_emitted.append({
                            "id": alert_id,
                            "alert_id": alert_id,
                            "event_id": eid,
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

            perf.record("postprocess", (time.perf_counter() - _t_post) * 1000.0)

            _t_emit = time.perf_counter()        # emit-stage start
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
                # Review-only/quarantined alerts remain on the Alerts page but
                # must not trigger the audible/browser real-time channel.
                from app.services.alert_quality import alert_is_suppressed
                if alert_is_suppressed(db, int(ev["alert_id"])):
                    continue
                try:
                    pub.publish("vg:pub:alerts", json.dumps(ev))
                except Exception:
                    pass

            # Telemetry: close the emit + total stages and tick the
            # frame counter (flushes percentiles every 100 frames).
            perf.record("emit", (time.perf_counter() - _t_emit) * 1000.0)
            perf.record("total", (time.perf_counter() - _t_iter) * 1000.0)
            perf.record_frame()

        time.sleep(poll_interval)
    finally:
        # Guarantees the tail window is flushed on BOTH exit paths
        # (max_seconds reached AND camera-gone / no-frame break).
        try:
            perf.emit_final()
        except Exception:
            pass
