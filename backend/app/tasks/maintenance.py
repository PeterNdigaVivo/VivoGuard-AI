"""Periodic maintenance tasks (clip retention, DDNS refresh, health rollup)."""
from __future__ import annotations
import logging
import socket

from app.tasks.celery_app import celery_app
from app.config import settings

log = logging.getLogger(__name__)


@celery_app.task(name="maintenance.refresh_ddns", ignore_result=True)
def refresh_ddns() -> None:
    """Re-resolve DDNS hostnames; if the IP changed, mark the camera so the
    streamer rebuilds its URL on next reconcile."""
    from app.database import SessionLocal
    from app.models import Camera
    with SessionLocal() as db:
        for cam in db.query(Camera).filter(Camera.ddns_hostname.isnot(None)):
            try:
                new_ip = socket.gethostbyname(cam.ddns_hostname)
                if cam.public_ip != new_ip:
                    log.info("DDNS %s: %s → %s", cam.ddns_hostname, cam.public_ip, new_ip)
                    cam.public_ip = new_ip
            except OSError as e:
                log.warning("DDNS resolve failed for %s: %s", cam.ddns_hostname, e)
        db.commit()


@celery_app.task(name="maintenance.prune_clips", ignore_result=True)
def prune_clips(retention_days: int = 30) -> None:
    """Placeholder — operators wire this up to their storage retention policy."""
    log.info("prune_clips retention_days=%s (stub — no-op)", retention_days)


@celery_app.task(name="maintenance.prune_alerts", ignore_result=True)
def prune_alerts(retention_days: int = 90) -> None:
    """Delete alerts + their detection_events (and snapshot files)
    older than `retention_days`. Keeps the alert history at a 90-day
    window for manager review without unbounded growth."""
    import os
    from datetime import datetime, timezone, timedelta
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    with SessionLocal() as db:
        old = (db.query(Alert, DetectionEvent)
                 .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                 .filter(DetectionEvent.timestamp < cutoff)
                 .limit(5000).all())
        for alert, ev in old:
            # Best-effort snapshot file cleanup.
            if ev.thumbnail_path:
                try:
                    os.remove(ev.thumbnail_path)
                except OSError:
                    pass
            db.delete(alert)
            removed += 1
        db.commit()
    log.info("prune_alerts: removed %d alerts older than %d days",
             removed, retention_days)


@celery_app.task(name="maintenance.bootstrap_buckets", ignore_result=True)
def bootstrap_buckets() -> None:
    """Ensure MinIO buckets exist."""
    from app.storage.minio_client import ensure_buckets
    ensure_buckets()
    log.info("bootstrap_buckets done (endpoint=%s)", settings.s3_endpoint)


@celery_app.task(name="maintenance.cameras_status_sync", ignore_result=True)
def cameras_status_sync() -> None:
    """Reconcile `cameras.status` with the actual streaming state.

    Until now this column was set on operator CRUD only — never by the
    runtime — so the dashboard's health pill drifted out of sync within
    minutes of a camera going up or down. We now sync it from the
    frame-buffer Redis key (`vg:frame:{id}`, TTL 30s):
        frame present       → status = "online"
        frame absent        → status = "offline"

    Operator-intent states (`pending`, `degraded`) are NEVER overwritten
    — they're set manually for cameras being onboarded or flagged for
    maintenance, and the sync respects them.

    Also: deletes orphaned `detection_configs` rows whose camera no
    longer exists (a camera DELETE relied on CASCADE which isn't always
    on, and even when on, the rows can accumulate from old migrations).
    """
    import redis as _redis_mod
    from app.database import SessionLocal
    from app.models import Camera
    from sqlalchemy import text

    try:
        r = _redis_mod.from_url(settings.redis_url)
    except Exception as e:
        log.warning("cameras_status_sync: redis unavailable: %s", e)
        return

    transitions_online  = 0
    transitions_offline = 0
    inspected           = 0
    with SessionLocal() as db:
        cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
        for cam in cams:
            inspected += 1
            try:
                frame_present = bool(r.get(f"vg:frame:{cam.id}"))
            except Exception:
                continue
            current = cam.status
            # Only manage online ↔ offline. Leave pending / degraded
            # alone — those are operator intent.
            if frame_present and current != "online":
                if current in ("offline",):
                    cam.status = "online"
                    transitions_online += 1
            elif not frame_present and current == "online":
                cam.status = "offline"
                transitions_offline += 1
        if transitions_online or transitions_offline:
            db.commit()

        # Orphaned detection_configs (camera deleted, config row left).
        # ON DELETE CASCADE handles this when the FK is enforced, but
        # legacy rows from before the constraint existed can persist.
        try:
            res = db.execute(text("""
                DELETE FROM detection_configs
                 WHERE camera_id NOT IN (SELECT id FROM cameras)
            """))
            orphans = res.rowcount or 0
            if orphans:
                db.commit()
                log.warning("cameras_status_sync: removed %d orphaned "
                            "detection_configs", orphans)
        except Exception as e:
            db.rollback()
            log.warning("cameras_status_sync: orphan cleanup failed: %s", e)
            orphans = 0

    log.info("cameras_status_sync: inspected=%d online+=%d offline+=%d orphans=%d",
             inspected, transitions_online, transitions_offline, orphans)
