"""Inference Celery tasks.

`run_camera_inference` is meant to be invoked once per camera; it loops
internally and exits when the camera is disabled or removed.
"""
from __future__ import annotations
import logging

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="inference.run_camera", bind=True, ignore_result=True)
def run_camera_inference(self, camera_id: int, *, max_seconds: int = 0) -> None:
    """Long-running task; one per camera. Spawn from API after CRUD changes."""
    from app.ai.inference_worker import run_for_camera
    log.info("run_camera_inference camera=%s max_seconds=%s", camera_id, max_seconds)
    try:
        run_for_camera(camera_id, max_seconds=max_seconds)
    except Exception as e:
        log.exception("run_camera_inference failed: %s", e)
        raise self.retry(exc=e, countdown=10, max_retries=3)
