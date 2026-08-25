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


def _safe_crontab(value: str, *, minute: int, hour: int):
    """Parse a five-field cron without allowing bad env to break startup."""
    try:
        parts = value.split()
        if len(parts) != 5:
            raise ValueError
        return crontab(minute=parts[0], hour=parts[1],
                       day_of_month=parts[2], month_of_year=parts[3],
                       day_of_week=parts[4])
    except (TypeError, ValueError):
        return crontab(minute=minute, hour=hour)


celery_app = Celery(
    "vivoguard",
    broker=settings.redis_url,
    backend=None,
    include=[
        "app.tasks.inference",
        "app.tasks.inference_watchdog",
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
        "app.tasks.vlm_tasks",
        "app.tasks.agents",
        "app.tasks.recorder",
        "app.tasks.alert_snapshots",
        "app.tasks.activity_sentinel",
        "app.tasks.uniform_miner",
        "app.tasks.system_health_report",
        "app.tasks.feedback_harvest",
        "app.tasks.operations_assurance",
        "app.tasks.scenario_simulation",
        "app.tasks.agent_accountability",
        "app.tasks.odoo_sync",
    ],
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Redis implements priorities using separate physical lists. Explicit
    # 0..9 steps let the latency-critical list pass the ordinary list as soon
    # as a worker is free. Keep the default round-robin strategy across named
    # queues so alert/Beat traffic cannot starve each other.
    task_queue_max_priority=9,
    broker_transport_options={
        "priority_steps": list(range(10)),
    },
    task_default_queue="default",
    # Auto-declare any queue referenced from a task route or an
    # `options={"queue": ...}` enqueue. Avoids having to enumerate
    # task_queues by hand.
    task_create_missing_queues=True,
    # Two dedicated worker pools (see docker-compose.yml):
    #   worker-inference  →  -Q inference,default  ( 8 slots by default)
    #   worker-alerts     →  -Q alerts,beat        ( 4 slots)
    # Routing the slow inference tasks into `inference` keeps them
    # out of the way of the short bookkeeping tasks on `alerts`,
    # which used to be starved when every default-queue slot was
    # held by a long camera loop.
    task_routes={
        # Inference pool.
        "inference.run_camera":               {"queue": "inference"},
        # Long-lived camera tasks deliberately fill the inference pool, so
        # supervisor heartbeats must run on the dedicated short-task runner.
        "inference.supervise_all":            {"queue": "beat"},
        # Operator-facing alerts pool.
        "alerting.sales_floor_insights_check":  {"queue": "alerts"},
        "alerting.store_intelligence_update":   {"queue": "alerts"},
        "alerting.live_activity_sentinel":      {"queue": "alerts"},
        "training.mine_live_uniform_crops":     {"queue": "alerts"},
        "alerting.sales_floor_daily_summary":   {"queue": "alerts"},
        "alerting.shop_not_opened_check":       {"queue": "alerts"},
        "alerting.shop_open_inference_check":   {"queue": "alerts"},
        "alerting.shop_daily_summary_check":    {"queue": "alerts"},
        "alerting.camera_health_check":         {"queue": "alerts"},
        "alerting.queue_escalation_check":      {"queue": "alerts"},
        "alerting.checkout_long_session_check": {"queue": "alerts"},
        "alerting.prune_checkout_snapshots":    {"queue": "alerts"},
        "vlm.analyse_alert_scene":              {"queue": "alerts"},
        "alerting.inference_pipeline_health_check": {"queue": "beat"},
        "inference.health_watchdog":               {"queue": "beat"},
        "alerting.uniform_violation_check":     {"queue": "alerts"},
        "alerting.after_hours_intrusion_check": {"queue": "alerts"},
        "alerting.after_hours_prune":           {"queue": "alerts"},
        "alerting.schedule_alert_filmstrip":    {"queue": "alerts"},
        "alerting.capture_filmstrip_frame":     {"queue": "alerts"},
        "alerting.prune_alert_snapshots":       {"queue": "alerts"},
        # Beat-only / scheduled batch tasks (also picked up by the
        # alerts worker — `beat` is on the same -Q list).
        "briefings.daily_fire_due":           {"queue": "beat"},
        "briefings.weekly_fire_due":          {"queue": "beat"},
        "training.chain_retrain_due":         {"queue": "beat"},
        "training.compute_model_metrics_daily": {"queue": "beat"},
        "training.pseudo_label_pending":      {"queue": "beat"},
        "training.weekly_retrain_all":        {"queue": "beat"},
        "training.evaluate_pending_promotions": {"queue": "beat"},
        "training.dispatch_queued_jobs":      {"queue": "beat"},
        # Status report rides `beat`, which now has a DEDICATED 1-slot
        # runner process (compose: beat-runner inside worker-alerts) —
        # training jobs filling the alerts pool starved it twice when
        # beat shared their slots.
        "system.daily_status_report":         {"queue": "beat"},
        "system.health_daily_report":         {"queue": "beat"},   # legacy alias
        # Heavy model fitting has its own worker. It must not consume alert
        # delivery capacity, and an alerts-worker restart must not kill a
        # multi-hour training process and blame the dataset.
        "training.run_job":                   {"queue": "training"},
        "training.write_preview_for_image":   {"queue": "alerts"},
        "training.backfill_previews":         {"queue": "alerts"},
        "training.harvest_temporal_frames":   {"queue": "alerts"},
        "training.run_shop_opening_specialist": {"queue": "alerts"},
        "training.run_store_specialist":      {"queue": "alerts"},
        "reports.dispatch_due":               {"queue": "beat"},
        "maintenance.refresh_ddns":           {"queue": "beat"},
        "maintenance.prune_alerts":           {"queue": "beat"},
        "maintenance.prune_metric_snapshots": {"queue": "beat"},
        "maintenance.cameras_status_sync":    {"queue": "beat"},
        "queue_report.fire_due":              {"queue": "beat"},
        "staff_classifier.classify_today":    {"queue": "beat"},
        "heatmap.snapshot_all":               {"queue": "beat"},
        "heatmap.snapshot_grids_hourly":      {"queue": "beat"},
        # Autonomous monitoring agents — ALL on the alerts pool so they
        # never compete with camera inference (RULE 5). Short (<60s) tasks.
        "agents.ml_dataset":       {"queue": "alerts"},
        "agents.training":         {"queue": "alerts"},
        "agents.backend_health":   {"queue": "alerts"},
        "agents.frontend":         {"queue": "alerts"},
        "agents.db_admin":         {"queue": "alerts"},
        "agents.streamer":         {"queue": "alerts"},
        "agents.simulation":       {"queue": "alerts"},
        "agents.detector_alerts":  {"queue": "alerts"},
        "agents.retail_standards": {"queue": "alerts"},
        "agents.inspection":       {"queue": "alerts"},
        "agents.agent_watchdog":   {"queue": "alerts"},
        "operations.coverage_assurance": {"queue": "alerts"},
        "operations.alert_quality":      {"queue": "alerts"},
        "operations.lone_worker":        {"queue": "alerts"},
        "operations.event_fusion":       {"queue": "alerts"},
        "operations.extract_recall_sample": {"queue": "alerts"},
        "operations.retention":          {"queue": "beat"},
        "odoo.sync_store_master":        {"queue": "beat"},
        "odoo.sync_roster":              {"queue": "beat"},
        "odoo.sync_pos_sessions":        {"queue": "beat"},
        "odoo.sync_sales_and_assurance": {"queue": "beat"},
        "agents.scenario_simulator":      {"queue": "alerts"},
        "agents.accountability":          {"queue": "alerts"},
        # Rolling recorder — runs in the dedicated `recorder` compose service
        # (celery worker -Q recorder), so ffmpeg survives worker rebuilds.
        "recorder.tick":                  {"queue": "recorder"},
        "recorder.extract_pending_clips": {"queue": "recorder"},
        "recorder.prune_source_recordings": {"queue": "recorder"},
        "recorder.prune_alert_clips":     {"queue": "recorder"},
        "recorder.backfill_evidence_hashes": {"queue": "recorder"},
        "recorder.storage_health_check":  {"queue": "recorder"},
    },
    # Pin Beat's clock to EAT (NOT settings.app_timezone, which is UTC on the
    # box) so the crontab-scheduled agents fire at their intended EAT times
    # (Retail Standards 05:00, Inspection 06:00, the 6h agents). Only crontab
    # schedules use this; interval (timedelta) schedules are timezone-agnostic.
    timezone="Africa/Nairobi",
    beat_schedule={
        "supervise-inference-every-30s": {
            "task": "inference.supervise_all",
            "schedule": 30.0,
        },
        "refresh-ddns-every-5min": {
            "task": "maintenance.refresh_ddns",
            "schedule": 300.0,
        },
        # Reconciles cameras.status from the Redis frame-buffer key so
        # the dashboard health pill matches reality. Also deletes
        # orphaned detection_configs whose camera no longer exists.
        # Routed to `beat` queue.
        "cameras-status-sync-every-5min": {
            "task": "maintenance.cameras_status_sync",
            "schedule": 300.0,
        },
        # Early-warning URGENT for inference-pipeline stalls. Reads the
        # vg:inference:health breadcrumb written by inference.supervise_all
        # every 30s; fires when it ages past 10 min. Per-30-min dedup.
        "inference-pipeline-health-every-60s": {
            "task": "alerting.inference_pipeline_health_check",
            "schedule": 60.0,
        },
        "inference-capacity-watchdog-every-60s": {
            "task": "inference.health_watchdog",
            "schedule": 60.0,
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
        # Checkout-long-session ATTENTION alert — scans Redis open-
        # session keys written by CheckoutDwellDetector and fires when
        # any live session exceeds settings.checkout_alert_minutes
        # (default 8). Per-zone 30-min dedup.
        "checkout-long-session-every-60s": {
            "task": "alerting.checkout_long_session_check",
            "schedule": 60.0,
        },
        # Hourly 24h retention sweep of checkout-dwell timeline
        # snapshot files (data/checkout_snaps/alerts/{alert_id}/).
        # Deletes files older than 24h from Alert.created_at AND
        # nulls Alert.snapshot_paths.
        "prune-checkout-snapshots-every-1h": {
            "task": "alerting.prune_checkout_snapshots",
            "schedule": 60 * 60.0,
        },
        # Backfill orange-box previews for TrainingImage rows created before
        # the opencv/API fix. Idempotent + self-limiting — no-ops once done,
        # so an hourly schedule effectively "runs once after deploy".
        "backfill-training-previews-every-1h": {
            "task": "training.backfill_previews",
            "schedule": 60 * 60.0,
        },
        # Business-hours alert filmstrips (data/alert_snaps/{store}/{alert}/).
        # Deletes JPEGs older than 48h by file mtime — every hour.
        "prune-alert-snapshots-every-1h": {
            "task": "alerting.prune_alert_snapshots",
            "schedule": 60 * 60.0,
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
        # After-hours intrusion filmstrip — every 60s, creates one intrusion
        # alert per closed store with a person present and attaches up to 6
        # snapshots (first immediately, then every 5 min). Reuses
        # Alert.snapshot_paths; per-store Redis session dedups.
        "after-hours-intrusion-every-60s": {
            "task": "alerting.after_hours_intrusion_check",
            "schedule": 60.0,
        },
        # 24h retention sweep for the after-hours filmstrip JPEGs.
        "after-hours-prune-every-1h": {
            "task": "alerting.after_hours_prune",
            "schedule": 60 * 60.0,
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
        # Per-model drift dashboard — every 15 min the task walks
        # the last 2 EAT days of alerts and recomputes precision /
        # fp_rate per (model_id, detection_type, day). Idempotent
        # upsert so late operator marks are picked up.
        "compute-model-metrics-every-15min": {
            "task": "training.compute_model_metrics_daily",
            "schedule": timedelta(minutes=15),
        },
        # Hourly batch pseudo-labelling — runs the deployed YOLO over
        # any TrainingImage rows still flagged labeled=False. High-
        # confidence detections become auto_suggested+verified
        # Annotations the trainer trusts; weaker frames are left for
        # operator review.
        "pseudo-label-hourly": {
            "task": "training.pseudo_label_pending",
            "schedule": timedelta(hours=1),
        },
        # Weekly self-learning orchestrator. Runs every 6 hours but
        # short-circuits when there aren't >= 50 new samples since
        # the last fine-tune per detection type — so a busy week
        # may produce one job per type, a quiet week none.
        "weekly-retrain-every-6h": {
            "task": "training.weekly_retrain_all",
            "schedule": timedelta(hours=6),
        },
        # Hourly promotion gate. Walks every non-deployed candidate
        # AIModel and flips deployment when its 7-day metrics beat
        # the sibling production model on precision + fp_rate.
        "evaluate-pending-promotions-hourly": {
            "task": "training.evaluate_pending_promotions",
            "schedule": timedelta(hours=1),
        },
        # The fix that completes the self-learning loop. Every 5 min,
        # picks up any TrainingJob rows sitting in status='queued',
        # marks them running, and dispatches them to the alerts
        # worker. Also sweeps stale (>12h) running rows to failed so
        # a crashed worker doesn't block the queue forever.
        "training-dispatcher-every-5min": {
            "task": "training.dispatch_queued_jobs",
            "schedule": timedelta(minutes=5),
        },
        # VivoGuard Status Report — the ONE daily email (11:30 EAT).
        # 5-min tick + wall-clock gate, sent-marker dedupe AFTER a
        # successful send, 15-min SMTP retries. Rides `beat`, which
        # has a dedicated 1-slot runner so heavy `alerts` work can
        # never delay it.
        "vivoguard-status-report-every-5min": {
            "task": "system.daily_status_report",
            "schedule": timedelta(minutes=5),
        },
        # Sales Floor Intelligence — 15-min timedelta tick (the
        # crontab schedule wasn't being picked up by this worker's
        # beat scheduler; switching to timedelta matches every other
        # interval task in this file).
        #
        # Routed to the `beat` queue so it doesn't compete with the
        # long-running inference.run_camera workers on the inference
        # queue. The
        # worker MUST be started with `-Q default,beat` to consume
        # both, otherwise messages pile up in `beat` forever.
        "sales-floor-insights-every-30min": {
            "task": "alerting.sales_floor_insights_check",
            "schedule": timedelta(minutes=15),
        },
        # Store Intelligence — one rich AI BI update per active store every
        # 45 min (Part 5). Tick every 15 min; the per-store vg:store_intel
        # key (2700s) enforces the 45-min cadence.
        "store-intelligence-every-15min": {
            "task": "alerting.store_intelligence_update",
            "schedule": timedelta(minutes=15),
        },
        # Live Activity Sentinel — reads the same vg:activity:* keys the
        # Live Activity tab uses and turns occupancy patterns into alerts.
        # Dark-launched: the task body no-ops unless
        # ACTIVITY_SENTINEL_ENABLED=true.
        "live-activity-sentinel": {
            "task": "alerting.live_activity_sentinel",
            "schedule": timedelta(seconds=int(getattr(
                settings, "activity_sentinel_interval_seconds", 60))),
        },
        # metric_snapshots retention — nightly at 03:10 EAT (quiet hours),
        # batched deletes; see maintenance.prune_metric_snapshots.
        "prune-metric-snapshots-nightly": {
            "task": "maintenance.prune_metric_snapshots",
            "schedule": crontab(minute=10, hour=3),
        },
        # Staff-uniform crop miner (Part 6) — mines live frames for training
        # data every 2 hours (30 cameras/run, rotating cursor).
        "uniform-crop-miner-every-2h": {
            "task": "training.mine_live_uniform_crops",
            "schedule": timedelta(hours=2),
        },
        # Daily 18:00 EAT WhatsApp summary — same routing rationale.
        "sales-floor-daily-summary-every-5min": {
            "task": "alerting.sales_floor_daily_summary",
            "schedule": timedelta(minutes=5),
        },
        # 5-min dispatcher checking whether each store has had its
        # first inward line-crossing of the day before the
        # not-opened cutoff (default 09:30 EAT). Per-store-per-day
        # Redis dedupe inside the task.
        "shop-not-opened-every-5min": {
            "task": "alerting.shop_not_opened_check",
            "schedule": timedelta(minutes=5),
        },
        # 1-min dispatcher inferring the store's opening time from
        # raw person detections when the entrance-crossing path
        # missed (wrong line direction, occlusion, dropped frames).
        # Fires INFO `shop_opened_inferred` with `opened_at` =
        # earliest event in the 5-min confirmation window. Per-store
        # marker is shared with the crossing path so the 09:30
        # URGENT check honours either signal.
        "shop-open-inference-every-1min": {
            "task": "alerting.shop_open_inference_check",
            "schedule": timedelta(minutes=1),
        },
        # 5-min dispatcher checking whether 22:00 EAT has passed for
        # each store; when it has, builds the daily open/close
        # summary from today's shop_open_close events and emits one
        # INFO alert + WhatsApp.
        "shop-daily-summary-every-5min": {
            "task": "alerting.shop_daily_summary_check",
            "schedule": timedelta(minutes=5),
        },

        # ── Autonomous monitoring agents ──────────────────────────────
        # Clock-aligned/daily agents use crontab() (celery timezone is
        # Africa/Nairobi = EAT, so hour= is EAT). Sub-hour agents use
        # plain intervals. Staggered per the resource plan so they never
        # wake up simultaneously. The watchdog re-enqueues any agent whose
        # heartbeat lapses — so if the embedded -B beat ever fails to pick
        # up a crontab entry, the agent still recovers.
        "agents-ml-dataset-6h": {          # 00:00 06:00 12:00 18:00 EAT
            "task": "agents.ml_dataset",
            "schedule": crontab(minute=0, hour="0,6,12,18"),
        },
        "agents-db-admin-6h": {            # 00:30 06:30 12:30 18:30 EAT
            "task": "agents.db_admin",
            "schedule": crontab(minute=30, hour="0,6,12,18"),
        },
        "agents-simulation-2h": {          # every 2 hours (Part 2 #4)
            "task": "agents.simulation",
            "schedule": crontab(minute=0, hour="*/2"),
        },
        "agents-training-1h": {            # :00 past every hour
            "task": "agents.training",
            "schedule": crontab(minute=0),
        },
        "agents-frontend-1h": {            # :15 past every hour
            "task": "agents.frontend",
            "schedule": crontab(minute=15),
        },
        "agents-retail-standards-daily": {  # 05:00 EAT
            "task": "agents.retail_standards",
            "schedule": crontab(minute=0, hour=5),
        },
        "agents-inspection-daily": {        # 06:00 EAT
            "task": "agents.inspection",
            "schedule": crontab(minute=0, hour=6),
        },
        "agents-backend-health-30min": {
            "task": "agents.backend_health",
            "schedule": timedelta(minutes=30),
        },
        "agents-streamer-5min": {
            "task": "agents.streamer",
            "schedule": timedelta(minutes=5),
        },
        "agents-detector-alerts-15min": {
            "task": "agents.detector_alerts",
            "schedule": timedelta(minutes=15),
        },
        "agents-watchdog-10min": {
            "task": "agents.agent_watchdog",
            "schedule": timedelta(minutes=10),
        },
        "operations-coverage-every-5min": {
            "task": "operations.coverage_assurance", "schedule": timedelta(minutes=5),
        },
        "operations-alert-quality-every-5min": {
            "task": "operations.alert_quality", "schedule": timedelta(minutes=5),
        },
        "operations-lone-worker-every-5min": {
            "task": "operations.lone_worker", "schedule": timedelta(minutes=5),
        },
        "operations-event-fusion-every-5min": {
            "task": "operations.event_fusion", "schedule": timedelta(minutes=5),
        },
        "operations-retention-daily": {
            "task": "operations.retention", "schedule": crontab(minute=40, hour=3),
        },
        # Odoo pull tasks are feature-flagged in their bodies and always fail
        # soft. They use the short-task pool and never block inference.
        "odoo-store-master-nightly": {
            "task": "odoo.sync_store_master",
            "schedule": _safe_crontab(settings.odoo_master_cron, minute=15, hour=2),
        },
        "odoo-roster-nightly": {
            "task": "odoo.sync_roster", "schedule": crontab(minute=35, hour=2),
        },
        "odoo-pos-sessions-every-15min": {
            "task": "odoo.sync_pos_sessions",
            "schedule": timedelta(minutes=settings.odoo_txn_minutes),
        },
        "odoo-sales-assurance-every-15min": {
            "task": "odoo.sync_sales_and_assurance",
            "schedule": timedelta(minutes=settings.odoo_txn_minutes),
        },
        "agents-scenario-simulator-hourly": {
            "task": "agents.scenario_simulator", "schedule": timedelta(hours=1),
        },
        "agents-accountability-every-5min": {
            "task": "agents.accountability", "schedule": timedelta(minutes=5),
        },

        # ── Rolling recorder ──────────────────────────────────────────
        # Interval tick (NOT crontab) — window start/end/delete is gated on
        # the EAT wall clock inside the task, so it's correct regardless of
        # celery's / the app's timezone. Executed by the
        # dedicated `recorder` worker.
        "recorder-tick-every-60s": {
            "task": "recorder.tick",
            "schedule": 60.0,
        },
        "recorder-extract-clips-every-60s": {
            "task": "recorder.extract_pending_clips",
            "schedule": 60.0,
        },
        "recorder-prune-source-recordings-hourly": {
            "task": "recorder.prune_source_recordings",
            "schedule": 60 * 60.0,
        },
        "recorder-prune-alert-clips-hourly": {
            "task": "recorder.prune_alert_clips",
            "schedule": 60 * 60.0,
        },
        "recorder-verify-legacy-evidence-every-30min": {
            "task": "recorder.backfill_evidence_hashes",
            "schedule": timedelta(minutes=30),
        },
        "recorder-storage-health-every-30min": {
            "task": "recorder.storage_health_check",
            "schedule": timedelta(minutes=30),
        },
    },
)
