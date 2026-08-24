"""System health endpoints — used by the System Health page (step 14)."""
from __future__ import annotations
import json
import shutil
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Alert, Camera, DetectionEvent
from app.stream.frame_buffer import FrameBuffer

router = APIRouter(prefix="/system", tags=["system"])

STREAM_FRESH_SECONDS = 10.0


def _proof_of_life_state(pipeline: dict | None, *, now: float) -> str:
    """Classify monitoring liveness without treating a quiet store as failed."""
    if not pipeline:
        return "offline"
    try:
        age = now - float(pipeline.get("last_run_ts") or 0)
    except (TypeError, ValueError):
        return "offline"
    if age > 10 * 60:
        return "offline"
    total = int(pipeline.get("cameras_total") or 0)
    fresh = int(pipeline.get("cameras_fresh") or 0)
    waiting = int(pipeline.get("cameras_waiting_for_worker") or 0)
    if total == 0 or fresh < total or waiting > 0:
        return "degraded"
    return "active"


def _runtime_camera_status(configured_status: str, health: dict,
                           *, now: float) -> tuple[str, float | None]:
    """Return stream health independently from operator workflow state.

    ``Camera.status`` also carries onboarding/maintenance intent (notably
    ``pending`` and ``degraded``), so presenting it as runtime health makes a
    camera with fresh frames look unavailable.  Redis frame telemetry is the
    source of truth for the System Health page; the configured state is still
    returned separately for operators who need it.
    """
    try:
        last_frame_at = float(health["last_frame_at"])
    except (KeyError, TypeError, ValueError):
        last_frame_at = None

    age = max(0.0, now - last_frame_at) if last_frame_at is not None else None
    try:
        fps = float(health.get("fps") or 0)
    except (TypeError, ValueError):
        fps = 0.0

    if age is not None and age < STREAM_FRESH_SECONDS and fps > 0:
        return "online", age
    if age is not None:
        return "stale", age
    if health.get("error"):
        return "offline", None
    if configured_status in {"offline", "pending", "degraded"}:
        return configured_status, None
    return "offline", None


def _decode_inference_pipeline(raw_pipeline) -> dict | None:
    """Decode supervisor telemetry without breaking the health endpoint."""
    try:
        if isinstance(raw_pipeline, bytes):
            raw_pipeline = raw_pipeline.decode()
        if not raw_pipeline:
            return None
        value = json.loads(raw_pipeline)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _inference_baseline(db: Session, *, now: datetime) -> dict:
    """Summarise 24h authoritative CPU telemetry for capacity comparison."""
    from app.models import InferencePerfLog

    rows = (
        db.query(
            func.count(func.distinct(InferencePerfLog.camera_id)),
            func.coalesce(func.sum(InferencePerfLog.frame_count), 0),
            func.max(InferencePerfLog.p95_ms),
            func.max(InferencePerfLog.p99_ms),
        )
        .filter(InferencePerfLog.timestamp >= now - timedelta(hours=24))
        .one()
    )
    return {
        "window_hours": 24,
        "cameras_reporting": int(rows[0] or 0),
        "frames": int(rows[1] or 0),
        "p95_ms_worst_camera_window": float(rows[2]) if rows[2] is not None else None,
        "p99_ms_worst_camera_window": float(rows[3]) if rows[3] is not None else None,
    }


def _capacity_acceptance(db: Session, fb: FrameBuffer, *, now: float) -> dict:
    from app.services.inference_acceptance import (
        CapacityThresholds,
        evaluate_capacity_acceptance,
    )

    authoritative = _decode_inference_pipeline(fb.r.get("vg:inference:health"))
    shadow = _decode_inference_pipeline(
        fb.r.get("vg:inference:batch-shadow-health"),
    )
    baseline = _inference_baseline(
        db, now=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    result = evaluate_capacity_acceptance(
        authoritative,
        shadow,
        now=now,
        baseline=baseline,
        thresholds=CapacityThresholds(
            max_health_age_seconds=settings.inference_batch_health_max_age_seconds,
            min_uptime_seconds=(
                settings.inference_batch_acceptance_min_uptime_seconds
            ),
            min_frames_per_camera=(
                settings.inference_batch_acceptance_min_frames_per_camera
            ),
            max_p95_per_frame_ms=(
                settings.inference_batch_acceptance_max_p95_ms
            ),
            max_schedule_wait_seconds=(
                settings.inference_batch_acceptance_max_wait_seconds
            ),
        ),
    )
    result["baseline"] = baseline
    return result


@router.get("/inference-acceptance")
def inference_acceptance(
    db: Session = Depends(get_db), _u=Depends(get_current_user),
):
    """Report capacity evidence without over-claiming alert accuracy."""
    return _capacity_acceptance(db, FrameBuffer(), now=time.time())


@router.get("/cameras/{camera_id}/stream-health")
def stream_health(camera_id: int, db: Session = Depends(get_db),
                  _u=Depends(get_current_user)):
    """What's the streamer doing for one camera? Used by Live View tiles
    to explain a black square instead of just showing it.

    Combines:
      - vg:health:{id}   (streamer's per-camera fps/error heartbeat)
      - vg:inference-hb:{id}  (worker's per-camera inference heartbeat)
      - Camera row metadata
    """
    import json
    import time as _t
    import redis as _redis
    from app.config import settings as _s
    from app.models import Camera

    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    r = _redis.from_url(_s.redis_url)
    raw_health = r.get(f"vg:health:{camera_id}")
    health = json.loads(raw_health) if raw_health else None
    hb_raw = r.get(f"vg:inference-hb:{camera_id}")
    inference_hb = float(hb_raw) if hb_raw else None

    now = _t.time()
    # last_frame_at = REAL JPEG arrival time (push_frame writes this).
    # last_health_at = any streamer status write (errors, retrying, etc).
    # is_streaming requires a REAL frame within the last 10 seconds.
    last_frame_at  = (health or {}).get("last_frame_at")
    last_health_at = (health or {}).get("last_health_at")
    fps            = (health or {}).get("fps") or 0
    is_streaming = (
        last_frame_at is not None
        and (now - float(last_frame_at)) < 10
        and fps > 0
    )

    return {
        "camera_id": cam.id,
        "camera_name": cam.name,
        "ai_enabled": cam.ai_enabled,
        "is_streaming": is_streaming,
        "fps": fps,
        "last_frame_at": last_frame_at,
        "last_health_at": last_health_at,
        "seconds_since_last_frame": (now - float(last_frame_at)) if last_frame_at else None,
        "seconds_since_last_health_write":
            (now - float(last_health_at)) if last_health_at else None,
        "error": (health or {}).get("error"),
        "inference_last_heartbeat": inference_hb,
        "inference_running": bool(inference_hb and (now - inference_hb) < 60),
    }


@router.get("/schema-check")
def schema_check(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """Compare actual DB columns to what the ORM expects. Use this to
    diagnose 'alembic claims head but rows still 500' situations."""
    from sqlalchemy import text
    expected = {
        "stores":  ["manager_name", "manager_phone", "business_hours_json", "capacity"],
        "cameras": ["store_id"],
    }
    out: dict = {}
    for table, cols in expected.items():
        rows = db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name=:t"), {"t": table}).fetchall()
        present = {r[0] for r in rows}
        out[table] = {
            "expected": {c: (c in present) for c in cols},
            "missing":  sorted(c for c in cols if c not in present),
        }
    try:
        alembic_current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        alembic_current = None
    out["alembic_current"] = alembic_current
    out["recommendation"] = (
        "If 'missing' is non-empty anywhere but alembic_current is the latest, "
        "the alembic_version table is ahead of reality. Recover with: "
        "`alembic stamp 0001 && alembic upgrade head` inside the api container."
    )
    return out


@router.get("/health")
def system_health(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    fb = FrameBuffer()
    cameras = db.query(Camera).filter(Camera.is_deleted.is_(False)).all()
    health_by_camera = fb.health_many([c.id for c in cameras])
    now = time.time()
    cam_health = []
    for c in cameras:
        h = health_by_camera.get(c.id, {})
        runtime_status, frame_age = _runtime_camera_status(
            c.status, h, now=now,
        )
        cam_health.append({
            "camera_id": c.id,
            "name":      c.name,
            "status":    runtime_status,
            "configured_status": c.status,
            "fps":       h.get("fps"),
            "last_frame_at": h.get("last_frame_at"),
            "seconds_since_last_frame": frame_age,
            "error":     None if runtime_status == "online" else (h.get("error") or c.last_error),
            "network_type": c.network_type,
        })

    # Disk usage on the recordings volume.
    du = shutil.disk_usage(settings.recordings_dir)

    # GPU info (best effort).
    gpu_info: list[dict] = []
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                p = torch.cuda.get_device_properties(i)
                free, total = torch.cuda.mem_get_info(i)
                gpu_info.append({
                    "index": i,
                    "name": p.name,
                    "total_mb": total // (1024 * 1024),
                    "free_mb":  free  // (1024 * 1024),
                })
    except Exception:
        pass

    new_alerts_24h = (db.query(func.count(Alert.id))
                        .filter(Alert.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
                        .scalar() or 0)

    inference_pipeline = _decode_inference_pipeline(
        fb.r.get("vg:inference:health"),
    )
    inference_batch_shadow = _decode_inference_pipeline(
        fb.r.get("vg:inference:batch-shadow-health"),
    )
    return {
        "now":            datetime.now(timezone.utc).isoformat(),
        "cameras":        cam_health,
        "disk_total_gb":  round(du.total / (1024 ** 3), 1),
        "disk_used_gb":   round(du.used  / (1024 ** 3), 1),
        "disk_free_gb":   round(du.free  / (1024 ** 3), 1),
        "gpus":           gpu_info,
        "alerts_today":   int(new_alerts_24h),
        "inference_pipeline": inference_pipeline,
        "inference_batch_shadow": inference_batch_shadow,
    }


@router.get("/proof-of-life")
def proof_of_life(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """Lightweight operator signal that distinguishes quiet from offline."""
    now_dt = datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    fb = FrameBuffer()
    pipeline = _decode_inference_pipeline(fb.r.get("vg:inference:health"))
    latest_detection = db.query(func.max(DetectionEvent.timestamp)).scalar()
    latest_alert = db.query(func.max(Alert.created_at)).scalar()

    def age_seconds(value: datetime | None) -> int | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, int((now_dt - value).total_seconds()))

    return {
        "now": now_dt.isoformat(),
        "state": _proof_of_life_state(pipeline, now=now_ts),
        "latest_detection_at": latest_detection.isoformat() if latest_detection else None,
        "latest_detection_age_seconds": age_seconds(latest_detection),
        "latest_alert_at": latest_alert.isoformat() if latest_alert else None,
        "latest_alert_age_seconds": age_seconds(latest_alert),
        "pipeline_age_seconds": (
            max(0, int(now_ts - float(pipeline.get("last_run_ts") or 0)))
            if pipeline and pipeline.get("last_run_ts") else None
        ),
        "cameras_total": int((pipeline or {}).get("cameras_total") or 0),
        "cameras_fresh": int((pipeline or {}).get("cameras_fresh") or 0),
        "cameras_actively_inferencing": (
            pipeline.get("cameras_actively_inferencing") if pipeline else None
        ),
        "cameras_waiting_for_worker": (
            pipeline.get("cameras_waiting_for_worker") if pipeline else None
        ),
        "inference_queue_depth": (
            pipeline.get("inference_queue_depth") if pipeline else None
        ),
        "estimated_full_rotation_seconds": (
            pipeline.get("estimated_full_rotation_seconds") if pipeline else None
        ),
    }
