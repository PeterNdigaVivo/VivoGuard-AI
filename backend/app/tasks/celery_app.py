"""Celery application — broker is Redis, results disabled (we don't need them)."""
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
)
