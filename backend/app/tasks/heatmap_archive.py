"""Daily heatmap PNG archive — 30-day rolling retention.

Beat fires `heatmap.snapshot_all` at 23:55 every day. For each active
camera with heatmap data in Redis we:
  1. Render the heatmap PNG (same colour ramp as the live thumbnail).
  2. Write it to /data/heatmaps/{camera_id}/{YYYY-MM-DD}.png.
  3. Insert/upsert a heatmap_snapshots row.
  4. Delete files + rows older than 30 days for that camera.
"""
from __future__ import annotations
import io
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import redis

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

RETENTION_DAYS = 30


def _archive_root() -> Path:
    p = Path(settings.recordings_dir) / "heatmaps"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _render_png(grid: list[list[int]], size: int, alpha: float = 0.85) -> bytes:
    """Same blue-cyan-yellow-red ramp the live endpoint uses, upscaled."""
    from PIL import Image
    if not grid:
        return b""
    max_v = max((max(row) for row in grid), default=0) or 1
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        for x in range(size):
            v = grid[y][x] / max_v
            if v <= 0:
                continue
            if v < 0.25:
                rgb = (0, int(v * 4 * 255), 255)
            elif v < 0.5:
                rgb = (0, 255, int((1 - (v - 0.25) * 4) * 255))
            elif v < 0.75:
                rgb = (int((v - 0.5) * 4 * 255), 255, 0)
            else:
                rgb = (255, int((1 - (v - 0.75) * 4) * 255), 0)
            px[x, y] = (*rgb, int(alpha * 255))
    img = img.resize((size * 16, size * 16), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@celery_app.task(name="heatmap.snapshot_all", ignore_result=True)
def snapshot_all() -> None:
    """Snapshot every active camera's heatmap to disk + index in DB."""
    from app.database import SessionLocal
    from app.models import Camera, HeatmapSnapshot
    r = redis.from_url(settings.redis_url)
    today = date.today()
    root = _archive_root()

    with SessionLocal() as db:
        cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
        for cam in cams:
            raw = r.get(f"vg:heatmap:{cam.id}")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            grid = payload.get("grid") or []
            n = payload.get("size") or len(grid) or 32
            if not grid:
                continue
            png = _render_png(grid, n)
            if not png:
                continue

            # Per-camera folder + dated filename.
            cam_dir = root / str(cam.id)
            cam_dir.mkdir(parents=True, exist_ok=True)
            file_path = cam_dir / f"{today.isoformat()}.png"
            file_path.write_bytes(png)

            peak = max((max(row) for row in grid), default=0)

            # Upsert (camera_id, day) — re-running on the same day overwrites.
            existing = (db.query(HeatmapSnapshot)
                          .filter(HeatmapSnapshot.camera_id == cam.id,
                                  HeatmapSnapshot.day == today)
                          .first())
            if existing:
                existing.file_path = str(file_path)
                existing.peak_value = int(peak)
                existing.store_id = cam.store_id
            else:
                db.add(HeatmapSnapshot(
                    camera_id=cam.id, store_id=cam.store_id,
                    day=today, file_path=str(file_path),
                    peak_value=int(peak),
                ))

            # Prune >30 days for this camera.
            cutoff = today - timedelta(days=RETENTION_DAYS)
            old = (db.query(HeatmapSnapshot)
                     .filter(HeatmapSnapshot.camera_id == cam.id,
                             HeatmapSnapshot.day < cutoff)
                     .all())
            for o in old:
                try:
                    Path(o.file_path).unlink(missing_ok=True)
                except Exception:
                    pass
                db.delete(o)

        db.commit()
        log.info("heatmap.snapshot_all: archived %d camera heatmaps for %s",
                 len(cams), today.isoformat())
