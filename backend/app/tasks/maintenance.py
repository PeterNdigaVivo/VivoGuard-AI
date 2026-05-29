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
