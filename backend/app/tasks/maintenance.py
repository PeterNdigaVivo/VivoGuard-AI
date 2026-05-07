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


@celery_app.task(name="maintenance.bootstrap_buckets", ignore_result=True)
def bootstrap_buckets() -> None:
    """Ensure MinIO buckets exist."""
    from app.storage.minio_client import ensure_buckets
    ensure_buckets()
    log.info("bootstrap_buckets done (endpoint=%s)", settings.s3_endpoint)
