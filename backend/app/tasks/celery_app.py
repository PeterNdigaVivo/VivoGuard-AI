"""Celery application — broker is Redis, results disabled (we don't need them).

Beat schedule:
  inference.supervise_all  — every 30 seconds, ensures one
                             `run_camera_inference` task is running for
                             each `ai_enabled` camera. Without this, no
                             inference ever runs, no detectors fire, no
                             metric_snapshots or DetectionEvent rows
                             land in Postgres.

Run the worker with `-B` so beat runs in the same process:
  celery -A app.tasks.celery_app worker -B --loglevel=info --concurrency=8
"""
from __future__ import annotations
from celery import Celery

from app.config import settings


celery_app = Celery(
    "vivoguard",
    broker=settings.redis_url,
    backend=None,
    include=[
        "app.tasks.inference",
        "app.tasks.training",
        "app.tasks.maintenance",
    ],
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    timezone=settings.app_timezone,
    beat_schedule={
        "supervise-inference-every-30s": {
            "task": "inference.supervise_all",
            "schedule": 30.0,
        },
        "refresh-ddns-every-5min": {
            "task": "maintenance.refresh_ddns",
            "schedule": 300.0,
        },
    },
)
