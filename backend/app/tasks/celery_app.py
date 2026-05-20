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
        "app.tasks.reports",
        "app.tasks.heatmap_archive",
        "app.tasks.staff_classifier",
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
        "scheduled-reports-dispatcher": {
            "task": "reports.dispatch_due",
            "schedule": 300.0,    # 5 min — granular enough for daily/weekly
        },
        # Daily heatmap archive at 23:55 (UTC). 30-day rolling retention
        # built into the task itself.
        "heatmap-archive-nightly": {
            "task": "heatmap.snapshot_all",
            "schedule": 24 * 60 * 60.0,
            # Celery's default scheduler doesn't support cron in plain
            # schedule= form; this fires roughly once per 24h relative
            # to worker boot. Acceptable — we just want one snapshot a day.
        },
        # Staff classifier — every 10 minutes, walk today's customer
        # journeys and flag any track that's accumulated >10 min in a
        # counter zone as 'staff'. The analytics endpoints LEFT-JOIN
        # staff_tracks and exclude those signatures.
        "staff-classifier-every-10min": {
            "task": "staff_classifier.classify_today",
            "schedule": 600.0,
        },
    },
)
