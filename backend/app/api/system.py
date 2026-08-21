"""System health endpoints — used by the System Health page (step 14)."""
from __future__ import annotations
import shutil
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Alert, Camera
from app.stream.frame_buffer import FrameBuffer

router = APIRouter(prefix="/system", tags=["system"])

STREAM_FRESH_SECONDS = 10.0


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

    return {
        "now":            datetime.now(timezone.utc).isoformat(),
        "cameras":        cam_health,
        "disk_total_gb":  round(du.total / (1024 ** 3), 1),
        "disk_used_gb":   round(du.used  / (1024 ** 3), 1),
        "disk_free_gb":   round(du.free  / (1024 ** 3), 1),
        "gpus":           gpu_info,
        "alerts_today":   int(new_alerts_24h),
    }
