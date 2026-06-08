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
from datetime import timedelta
from celery import Celery
from celery.schedules import crontab  # noqa: F401  (kept for other tasks that may use it)

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
        "app.tasks.briefings",
        "app.tasks.alerting",
        "app.tasks.shutter_training",
        "app.tasks.uniform_training",
        "app.tasks.chain_training",
        "app.tasks.queue_report",
    ],
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    # Auto-declare any queue referenced by `options={"queue": ...}` on
    # an enqueue so we don't have to enumerate task_queues by hand.
    # The bookkeeping beat tasks route to `beat` to avoid being
    # starved by the 16 long-running inference.run_camera tasks
    # holding every slot on `default`.
    task_create_missing_queues=True,
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
        # Alert history retention — prune alerts + snapshots older than
        # 90 days once a day (relative to worker boot).
        "prune-alerts-daily": {
            "task": "maintenance.prune_alerts",
            "schedule": 24 * 60 * 60.0,
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
        # Hourly grid snapshots for the replay timeline. 90-day
        # retention is enforced inside the task itself.
        "heatmap-grid-snapshots-hourly": {
            "task": "heatmap.snapshot_grids_hourly",
            "schedule": 60 * 60.0,
        },
        # Staff classifier — every 10 minutes, walk today's customer
        # journeys and flag any track that's accumulated >10 min in a
        # counter zone as 'staff'. The analytics endpoints LEFT-JOIN
        # staff_tracks and exclude those signatures.
        "staff-classifier-every-10min": {
            "task": "staff_classifier.classify_today",
            "schedule": 600.0,
        },
        # Daily WhatsApp briefing per store — fires at 08:00 store-local
        # for each active store. The dispatcher checks the local clock
        # every 5 minutes and uses a Redis day-marker to dedup.
        "briefings-daily-every-5min": {
            "task": "briefings.daily_fire_due",
            "schedule": 300.0,
        },
        # Weekly chain briefing — fires Monday 07:00 anchor-time.
        # Same 5-minute beat tick + iso-week marker for dedup.
        "briefings-weekly-every-5min": {
            "task": "briefings.weekly_fire_due",
            "schedule": 300.0,
        },
        # Sustained-queue WhatsApp escalation — every 30s the task
        # checks the latest queue_length snapshot per zone. Fires once
        # per zone when count > 5 has held for > 3 min.
        "queue-escalation-every-30s": {
            "task": "alerting.queue_escalation_check",
            "schedule": 30.0,
        },
        # Camera-offline WhatsApp nudge — every 60s the task scans
        # ai_enabled cameras and fires when last_seen is > 5 min stale
        # AND the store is currently within business hours.
        "camera-health-every-60s": {
            "task": "alerting.camera_health_check",
            "schedule": 60.0,
        },
        # Uniform-violation manager notification — every 60s scans for
        # uniform_compliance alerts in the last ~2 min and WhatsApps the
        # store manager. Deduped per store per 30 min.
        "uniform-violation-every-60s": {
            "task": "alerting.uniform_violation_check",
            "schedule": 60.0,
        },
        # Daily Queue Intelligence report — fires once per store after
        # 21:00 store-local. 5-min beat tick + per-store Redis dedup.
        "queue-report-every-5min": {
            "task": "queue_report.fire_due",
            "schedule": 300.0,
        },
        # Weekly chain auto-retrain — 5-min beat tick. Fires Monday
        # 02:00 Africa/Nairobi when the chain dataset has grown since
        # the last trained chain model. iso-week marker for dedup.
        "chain-retrain-every-5min": {
            "task": "training.chain_retrain_due",
            "schedule": 300.0,
        },
        # Sales Floor Intelligence — 15-min timedelta tick (the
        # crontab schedule wasn't being picked up by this worker's
        # beat scheduler; switching to timedelta matches every other
        # interval task in this file).
        #
        # Routed to the `beat` queue so it doesn't compete with the
        # 16 long-running inference.run_camera workers on the
        # `default` queue (each holding a slot for ~9 minutes). The
        # worker MUST be started with `-Q default,beat` to consume
        # both, otherwise messages pile up in `beat` forever.
        "sales-floor-insights-every-15min": {
            "task": "alerting.sales_floor_insights_check",
            "schedule": timedelta(minutes=15),
            "options": {"queue": "beat"},
        },
        # Daily 18:00 EAT WhatsApp summary — same routing rationale.
        "sales-floor-daily-summary-every-5min": {
            "task": "alerting.sales_floor_daily_summary",
            "schedule": timedelta(minutes=5),
            "options": {"queue": "beat"},
        },
        # 5-min dispatcher checking whether each store has had its
        # first inward line-crossing of the day before the
        # not-opened cutoff (default 09:30 EAT). Per-store-per-day
        # Redis dedupe inside the task.
        "shop-not-opened-every-5min": {
            "task": "alerting.shop_not_opened_check",
            "schedule": timedelta(minutes=5),
            "options": {"queue": "beat"},
        },
        # 5-min dispatcher checking whether 22:00 EAT has passed for
        # each store; when it has, builds the daily open/close
        # summary from today's shop_open_close events and emits one
        # INFO alert + WhatsApp.
        "shop-daily-summary-every-5min": {
            "task": "alerting.shop_daily_summary_check",
            "schedule": timedelta(minutes=5),
            "options": {"queue": "beat"},
        },
    },
)
