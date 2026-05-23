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
    """Premier League-style navy → blue → orange → red ramp.

    Mirrors the live endpoint's renderer so archived PNGs match what
    operators see on the dashboard. Uses nearest-neighbour upscaling
    + a bloom ring on the hottest cell for the football-analytics
    look (sharp banding rather than a smooth jet gradient).
    """
    from PIL import Image, ImageDraw
    # Re-import the live renderer's colour ramp + threshold so the
    # archive PNG matches the dashboard pixel-for-pixel. Local import
    # avoids a circular dep at module load.
    from app.api.analytics import _heatmap_color, _HEATMAP_HOTSPOT_THRESHOLD
    if not grid:
        return b""
    max_v = max((max(row) for row in grid), default=0) or 1
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    hotspots: list[tuple[int, int]] = []
    for y in range(size):
        for x in range(size):
            v = grid[y][x] / max_v
            if v <= 0:
                continue
            r_, g_, b_ = _heatmap_color(v)
            a = int(alpha * 255 * (0.7 + 0.3 * v))
            if v >= _HEATMAP_HOTSPOT_THRESHOLD:
                a = 255
                hotspots.append((x, y))
            px[x, y] = (r_, g_, b_, min(255, a))
    img = img.resize((size * 16, size * 16), Image.NEAREST)
    if hotspots and len(hotspots) < (size * size) // 4:
        draw = ImageDraw.Draw(img)
        mx, my, mv = hotspots[0][0], hotspots[0][1], grid[hotspots[0][1]][hotspots[0][0]]
        for (x, y) in hotspots:
            if grid[y][x] > mv:
                mx, my, mv = x, y, grid[y][x]
        cx = mx * 16 + 8
        cy = my * 16 + 8
        for radius, width in ((28, 3), (40, 2)):
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                         outline=(255, 255, 255, 220), width=width)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


GRID_SNAPSHOT_RETENTION_DAYS = 90
SNAPSHOT_LAYERS = ("traffic", "engagement", "congestion", "paths")


@celery_app.task(name="heatmap.snapshot_grids_hourly", ignore_result=True)
def snapshot_grids_hourly() -> None:
    """Hourly capture of all four heatmap layers per camera into the
    `heatmap_grid_snapshots` table. Drives the replay timeline.

    Pulls the DAY-window grids from Redis (which by definition cover
    the period 00:00 -> now), so the replay shows cumulative state at
    that hour. 90-day retention enforced inline.
    """
    from datetime import datetime, timezone
    from app.database import SessionLocal
    from app.models import Camera, HeatmapGridSnapshot
    r = redis.from_url(settings.redis_url)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=GRID_SNAPSHOT_RETENTION_DAYS)

    with SessionLocal() as db:
        cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
        for cam in cams:
            for layer in SNAPSHOT_LAYERS:
                key = (f"vg:heatmap:{layer}:day:{cam.id}" if layer != "traffic"
                       else f"vg:heatmap:day:{cam.id}")
                raw = r.get(key)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if layer == "paths":
                    grid_data = payload.get("edges") or []
                    peak = float(max((e["count"] for e in grid_data), default=0))
                else:
                    grid = payload.get("grid") or []
                    grid_data = grid
                    peak = float(max((max(row) for row in grid), default=0))
                db.add(HeatmapGridSnapshot(
                    camera_id=cam.id,
                    store_id=cam.store_id,
                    captured_at=now,
                    hour=now.hour,
                    layer=layer,
                    grid_data=grid_data,
                    peak_value=peak,
                ))
        db.commit()
        # Retention sweep.
        db.query(HeatmapGridSnapshot).filter(
            HeatmapGridSnapshot.captured_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()


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
