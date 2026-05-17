"""System health endpoints — used by the System Health page (step 14)."""
from __future__ import annotations
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Alert, Camera
from app.stream.frame_buffer import FrameBuffer

router = APIRouter(prefix="/system", tags=["system"])


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
    cameras = db.query(Camera).all()
    cam_health = []
    for c in cameras:
        h = fb.health(c.id) or {}
        cam_health.append({
            "camera_id": c.id,
            "name":      c.name,
            "status":    c.status,
            "fps":       h.get("fps"),
            "last_frame_at": h.get("last_frame_at"),
            "error":     h.get("error") or c.last_error,
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
