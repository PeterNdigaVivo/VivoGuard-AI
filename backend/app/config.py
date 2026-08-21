"""Application configuration loaded from environment variables.

Single source of truth for every tunable. Imported via `from app.config
import settings` everywhere else; never read os.environ directly.
"""
from functools import lru_cache
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-backed settings. Names match keys in `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",       # tolerate unknown vars (compose injects extras)
        case_sensitive=False,
    )

    # --- Core ---
    app_env: str = "production"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # Default timezone for the application. Drives:
    #   • Celery beat's clock (so 21:00 daily reports fire at 21:00 EAT)
    #   • Log timestamps from the api / worker / streamer containers
    #   • Wall-clock comparisons in the scheduled-report dispatcher
    #     when a store has no timezone of its own
    # Per-STORE timezone (stores.timezone) still wins for per-store
    # business-hours math — this is just the chain-wide default.
    app_timezone: str = "Africa/Nairobi"

    # --- Auth ---
    jwt_secret: str = Field(default="dev-only-change-me", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60
    jwt_refresh_ttl_days: int = 30
    # Odoo signs ``<unix timestamp>.<raw body>`` with HMAC-SHA256.
    # An empty secret keeps the service endpoint unavailable, never unsigned.
    odoo_webhook_secret: str = ""
    odoo_webhook_max_age_seconds: int = 300
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "change-me-now"

    # --- Postgres ---
    # Set DATABASE_URL to override the postgres_* fields entirely (e.g.
    # sqlite:///./edge.db on the edge appliance, or a managed RDS URL).
    database_url_override: str = Field(default="",
                                       validation_alias=AliasChoices("DATABASE_URL", "database_url"))
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "vivoguard"
    postgres_user: str = "vivoguard"
    postgres_password: str = "vivoguard"

    # --- Redis ---
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # --- Object storage (MinIO/S3) ---
    s3_endpoint: str = "http://minio:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket_clips: str = "clips"
    s3_bucket_thumbs: str = "thumbnails"
    s3_bucket_models: str = "models"
    s3_bucket_datasets: str = "datasets"
    s3_use_ssl: bool = False

    # --- Encryption (Fernet) ---
    credentials_fernet_key: str = ""

    # --- AI / GPU ---
    # `use_gpu` is retained for the training tasks (trainer / evaluator /
    # chain / shutter / uniform) which still pass device="0"|"cpu" to
    # YOLO.train(). Inference now auto-detects via env_config.HardwareEnv
    # and ignores this flag.
    use_gpu: bool = False
    cuda_visible_devices: str = "0"
    # Which compute backend the worker IMAGE was built for. Drives the
    # Docker build (requirements.worker.{backend}.txt) and is logged at
    # startup; the runtime still auto-detects actual hardware via
    # env_config.HardwareEnv.detect(). cpu | cuda | rocm | mps | intel.
    gpu_backend: str = "cpu"
    # One-time export of the model to the detected backend's optimized
    # format (TensorRT/ONNX/OpenVINO/CoreML), then load the cached
    # artifact. False = always raw PyTorch (simpler for debugging).
    use_optimized: bool = True
    default_model: str = "yolov8n.pt"
    # When True, cameras with no explicit model use the latest DEPLOYED model
    # for inference. OFF by default: the deployed retail model (e.g. v25) is a
    # specialist whose class names are NOT COCO "person", so using it for the
    # general inference pass makes every person-based detector see 0 persons
    # (chain-wide outage, Jul 2026). Only enable once the deployed model is
    # confirmed to emit a COCO-"person" class.
    use_deployed_model_for_inference: bool = False
    # Cross-store generalist model (top-3 stores). Full train from base
    # yolov8n by default (a generalist person/retail detector — NOT fine-tuned
    # from the specialist deployed model). Auto-deploy is OFF: a >=0.85 model
    # is staged deployed=false and requires a manual promote, given the recent
    # deployed-model outage.
    cross_store_base_model:   str   = "yolov8n.pt"
    cross_store_promote_map50: float = 0.85
    cross_store_auto_deploy:  bool  = False
    # ---- training-pipeline hardening (ML audit) --------------------------
    # Minimum COMBINED dataset size (positives + capped negatives + replay
    # mix, post-sanitization) a training job may run with. Below this the
    # trainer aborts with InsufficientDataError; the orchestrator projects
    # the same number before enqueueing so doomed jobs never queue.
    min_training_images: int = 50
    # Anti-catastrophic-forgetting replay: fraction of the parent model's
    # original dataset sampled into every fine-tune (train split only).
    # 0.18 sits inside the standard 15-20% replay band.
    base_mix_fraction: float = 0.18
    # Variance-of-Laplacian floor for the sanitize pre-check — images
    # scoring below are excluded as severely blurred.
    sanitize_blur_threshold: float = 25.0
    # Max absolute map50 drop an automated promotion tolerates versus the
    # deployed baseline. 0.0 = strict no-regression (historical behaviour);
    # 0.05 relaxes to the standard "no worse than 5 points" gate.
    model_gate_max_regression: float = 0.0
    # Training-job stall watchdog: a `running` job with no epoch
    # progress (last_progress_at heartbeat) for this many minutes is
    # revoked and requeued by the dispatcher (max 2 requeues, then
    # failed). Cross-store trains complete in ~30-60 min; 90 gives
    # headroom for dataset prep + first epoch. 0 disables the watchdog.
    training_stall_timeout_minutes: int = 90
    # A `running` job whose heartbeat was NEVER stamped (the trainer's
    # first act) is a task the worker never picked up — requeue after
    # this many seconds instead of waiting the full stall timeout.
    # 0 disables the fast tier.
    training_start_timeout_seconds: int = 60
    # ---- aggressive feedback-driven retraining (Aug 2026) ----------------
    # After this many new True/False clicks on a detection type since its
    # last completed job → queue an incremental fine-tune immediately.
    feedback_finetune_after: int = 10
    # After this many → queue a FULL retrain instead.
    feedback_full_retrain_after: int = 30
    # Feedback fine-tunes auto-deploy (registry-level) when the new map50
    # beats the parent's by at least this margin AND the model gate passes.
    # NOTE: with use_deployed_model_for_inference=False this changes the
    # registry/promotion chain, not live inference — cameras follow their
    # explicit ai_model_id assignment.
    feedback_auto_promote_margin: float = 0.02
    # Temporal alert gate: minimum consecutive tracked frames before an
    # alert fires (event rows always persist). Applies only to detectors
    # WITHOUT their own frame gates; entry_exit / shop_open_close exempt.
    # At the platform's 1-2 fps, 10 frames ≈ 5-10s — ops runs 4.
    temporal_gate_min_frames: int = 10
    # Retention for the metric_snapshots time-series table (days). The
    # dashboards' largest window is 30 days; 90 keeps triple margin.
    # 0 disables pruning entirely.
    metric_retention_days: int = 90
    # Additive incident lifecycle/evidence projection. Both switches are off
    # by default so deploying migration 0040 cannot change alert behaviour.
    incident_foundations_enabled: bool = False
    delivery_outbox_enabled: bool = False
    # ---- Live Activity Sentinel (dark-launched) --------------------------
    # Consumes the vg:activity:* keys the Live Activity tab reads and
    # turns occupancy patterns into alerts. Per-camera overrides live in
    # detection_configs rows with detection_type="live_activity".
    activity_sentinel_enabled: bool = False
    activity_surge_people: int = 12
    activity_surge_sustain_samples: int = 3
    activity_store_surge_people: int = 30
    activity_dead_scene_minutes: int = 0          # 0 = dead_scene rule off
    activity_sentinel_interval_seconds: int = 60
    # activity_presence rule — INFO alert on sustained activity
    # (people >= threshold for N samples). Threshold 5 = the minimum
    # people count required to trigger a presence alert, filtering out
    # passersby / single browsers; the per-camera 10-min dedupe bucket
    # bounds the rate.
    # Static-object (mannequin) filter for the activity feed: a tracked
    # person whose bbox centre moved less than this many pixels across
    # the last N frames is treated as a fixture and excluded from the
    # people count, the sentinel rules, and alert snapshots.
    # mannequin_filter_enabled is the master switch (MANNEQUIN_FILTER_ENABLED).
    mannequin_filter_enabled: bool = True
    activity_static_displacement_px: int = 5
    activity_static_window_frames: int = 10
    # Keep recently tracked people through short YOLO misses/occlusions.
    # Alert detectors still evaluate only the current frame.
    activity_track_hold_seconds: float = 5.0
    activity_presence_enabled: bool = True
    activity_presence_threshold: int = 5
    activity_presence_sustain_samples: int = 2
    # 2 fps per camera by default — comfortably handles 40+ cameras on
    # CPU. Bump per camera via Camera.inference_fps if you need finer
    # tracking on a high-priority camera. Accepts INFERENCE_FPS (new
    # name in .env.example) or INFERENCE_FPS_DEFAULT (legacy) from env.
    inference_fps_default: int = Field(
        default=2,
        validation_alias=AliasChoices("INFERENCE_FPS",
                                       "INFERENCE_FPS_DEFAULT",
                                       "inference_fps_default"),
    )

    # --- VLM scene understanding (Sprint 2.1) ---
    # Cloud Vision-LM that adds a natural-language scene description to
    # alert-worthy events. Runs ASYNC on the alerts queue (never on the
    # inference hot path). Off by default; needs anthropic_api_key.
    vlm_enabled: bool = False
    vlm_provider: str = "anthropic"
    vlm_model: str = "claude-haiku-4-5"
    # Secret — env-only, never logged or committed.
    anthropic_api_key: str = ""
    vlm_timeout_seconds: int = 10

    # ── Autonomous AI monitoring agents (app/tasks/agents.py) ──────────
    # When enabled (and anthropic_api_key is set), each domain agent hands
    # its deterministic telemetry to Claude for reasoning/diagnosis and
    # natural-language recommendations. When disabled or the API is
    # unreachable, agents fall back to their rule-based verdict so they
    # never break. Shares anthropic_api_key with the VLM.
    agents_llm_enabled: bool = True
    # Default (Sonnet) model — used by the two daily strategic agents
    # (retail standards, inspection). The analytical agents override to
    # claude-haiku-4-5 in agents.py.
    agents_llm_model: str = "claude-sonnet-4-6"
    # Emit a silent, resolved, evidence-backed in-app alert only when an
    # autonomous agent transitions from warning/critical back to ok.
    positive_agent_alerts_enabled: bool = True
    positive_agent_alert_dedup_hours: int = Field(default=6, ge=1, le=168)
    agents_llm_timeout_seconds: int = 45
    vlm_alert_types: list[str] = Field(
        default_factory=lambda: [
            "checkout_dwell", "staff_present", "trespass",
            "uniform_compliance", "shop_open_close",
        ],
        validation_alias=AliasChoices("VLM_ALERT_TYPES", "vlm_alert_types"),
    )

    @field_validator("vlm_alert_types", mode="before")
    @classmethod
    def _split_vlm_alert_types(cls, v):
        # Accept a comma-separated env string OR a JSON list. A bare
        # comma list (VLM_ALERT_TYPES=a,b,c) is friendlier than JSON in
        # .env, so coerce it here.
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                import json
                try:
                    return json.loads(s)
                except Exception:
                    pass
            return [t.strip() for t in s.split(",") if t.strip()]
        return v

    # --- Cross-camera person Re-ID (Sprint 2.2) ---
    # Matches the SAME person across cameras via appearance embeddings so
    # we count unique visitors (not per-camera appearances) and can draw
    # customer journeys. Runs on the worker via a CPU ResNet18 embedder,
    # only every Nth frame per track — negligible load. Off by default;
    # needs torchvision (present in the CPU worker image).
    reid_enabled: bool = False
    # Cosine-similarity cut-off for "same person". Higher = stricter
    # (fewer false merges, more split identities). 0.70 suits the HSV
    # colour-histogram encoder — slightly lower than a deep-embedding
    # default because a colour descriptor is less discriminative. Tune
    # per deployment.
    reid_match_threshold: float = 0.70
    # How long a gallery entry survives without a re-match (seconds).
    # 4h covers a long shopping trip; after that the person is treated
    # as a new visitor. Env: REID_GALLERY_TTL_SECONDS.
    reid_gallery_ttl_seconds: int = 14400
    # Encode 1 of every N frames per track to bound CPU cost (ResNet18 is
    # ~15-30ms/crop on CPU). Env: REID_ENCODE_EVERY_N_FRAMES.
    reid_encode_every_n_frames: int = 5
    # Soft cap on gallery entries per store; oldest are evicted past this.
    reid_max_gallery_size: int = 500
    # Duration of one per-camera inference task before it exits and the
    # supervisor re-queues it. Lowered from the old hard-coded 540s to
    # 120s so a config/zone change reaches a camera within ~2 min
    # instead of up to 9. The task ALWAYS releases its Redis lock on
    # exit (inference.py finally-block), so the supervisor re-acquires
    # immediately; LOCK_TTL tracks this value + 60s purely as a
    # crashed-worker safety net. Env: INFERENCE_RUN_SECONDS.
    inference_run_seconds: int = 120

    # --- Notifications ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "alerts@vivoguard.local"
    smtp_use_tls: bool = True
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    # WhatsApp via Twilio Business API.
    twilio_whatsapp_from: str = ""       # e.g. "whatsapp:+14155238886"
    whatsapp_to: str = ""                # comma-separated "whatsapp:+254..."
    whatsapp_priority_only: bool = True  # only high-priority alerts by default
    # Weekly chain-briefing recipients (Monday 07:00). Comma-separated
    # `whatsapp:+<msisdn>` numbers. Empty = no weekly briefing sent.
    weekly_briefing_to: str = ""
    # Dashboard escalation recipient — sustained queue + camera-health
    # alerts go here regardless of per-store manager_phone wiring. The
    # ops team wanted a single number that gets every high-priority
    # nudge from the dashboard.
    dashboard_alert_to: str = "whatsapp:+25441418586"
    # Phone numbers surfaced in the "What to do" alert steps so the
    # guidance is actionable. Blank = the card shows a generic phrase
    # ("building security", "IT support", "the store").
    security_phone: str = ""
    it_support_phone: str = ""
    store_default_phone: str = ""
    webhook_url: str = ""
    webhook_auth_header: str = ""

    # --- After-hours person alert tuning ---
    # Pre-opening grace: staff arriving up to N minutes before opening
    # don't trigger an URGENT "Person Detected After Hours" alert.
    person_afterhours_grace_before_min: int = 60
    # Symmetric post-closing grace for end-of-day staff egress.
    person_afterhours_grace_after_min: int = 60
    # A confirmed shop_closed event can occur later than the scheduled
    # close. Allow routine staff egress for 30 minutes after that observed
    # closure before escalating a person as an after-hours intrusion.
    person_afterhours_actual_close_grace_min: int = 30
    # Per-camera dedupe — once an after-hours person alert has fired
    # for a camera, suppress repeats for this many minutes.
    person_afterhours_dedupe_min: int = 30

    # --- Sales-floor insight debugging ---
    # When True, sales_floor_insights_check skips the 15-min Redis
    # dedupe AND the closed-hours gate AND the "store needs an aisle
    # zone" gate — every active store with at least one camera gets
    # an alert each tick. Use to confirm the task is wired up after
    # a deploy / DB restore where zones may not have been synced.
    sales_floor_debug: bool = False
    # Tracking-accuracy gates (ByteTrack P5) — frame-count / confidence
    # minimums per detector, tunable via env.
    checkout_min_frames:            int   = 5
    uniform_confidence_threshold:   float = 0.65
    trespass_min_frames:            int   = 5
    counter_min_empty_frames:       int   = 10
    entry_exit_min_frames_per_side: int   = 1   # cameras infer in short bursts;
                                                # a real crossing is only 1-2
                                                # frames — trust ByteTrack's
                                                # track_id side transition.
    # Part 5: the legacy sales-floor heartbeat is OFF by default — replaced by
    # the store_intelligence 45-min BI update. Flip via env to re-enable.
    sales_floor_insight_enabled: bool = False   # intentionally OFF (Aug 2026)
    # Re-enabled Aug 2026 — the Jul-2026 pause default silently emptied
    # the Store Update tab for a month (last event Jul 23). The task
    # now logs every generation so a silent disable can't recur.
    store_intelligence_enabled:  bool = True
    store_intel_llm_model: str = "claude-haiku-4-5"

    # --- ROI / Value Report tuning ---
    # Per-incident KES values used by /analytics/roi to estimate the
    # value VivoGuard delivered this month. Conservative defaults —
    # head office tunes via .env for their own pricing assumptions.
    roi_theft_per_incident_kes:        int = 30000
    roi_unauthorised_per_incident_kes: int = 8000
    roi_queue_per_incident_kes:        int = 2000
    # Monthly cost of running VivoGuard, used as the denominator in
    # the ROI multiple. Set to the contracted SaaS + infra figure.
    roi_monthly_cost_kes:              int = 45000

    # --- Shutter open/close detection method ---
    # When True, ShutterDetector runs its brightness + texture
    # rule-based classifier AND requires shutter=open before the
    # "Store opened" alert fires (the original two-signal design).
    # When False (default), the brightness analysis is skipped
    # entirely and the line-crossing alone drives shop-open/close
    # alerts — overhead cameras with messy lighting can't lie about
    # whether the shutter is up if no-one's walked in.
    # Kept as a flag so the brightness method can be A/B-tested
    # later without re-writing the code.
    use_shutter_brightness: bool = False

    # --- WAN behaviour ---
    ddns_refresh_interval_seconds: int = 300
    wan_reconnect_initial_delay_seconds: int = 5
    wan_reconnect_max_delay_seconds: int = 300
    wan_offline_alert_threshold_seconds: int = 120

    # --- Auto-retrain triggers ---
    # The self-learning pipeline fires an incremental fine-tune when
    # EITHER condition is met:
    #   - new positive+negative feedback samples since last fine-tune
    #     ≥ retrain_min_feedback (default 20), OR
    #   - calendar days since last fine-tune ≥ retrain_max_days
    #     (weekly cadence at default 7).
    # Env-overridable via RETRAIN_MIN_FEEDBACK / RETRAIN_MAX_DAYS.
    retrain_min_feedback: int = 20
    retrain_max_days: int = 7

    # --- Glass-door detector tuning ---
    # Minimum frames a pseudo-track must be matched on before a side-
    # flip on a glass_door zone is allowed to fire a crossing.
    # At the default 1-2 fps inference rate a person walks through the
    # door in a single frame, so a >1 requirement blocks every real
    # crossing; the 0.65-confidence threshold is what actually filters
    # reflections. Raise this to 2 only if inference fps is raised
    # above ~2 fps where a real person reliably appears in multiple
    # consecutive frames. Env: GLASS_DOOR_MIN_FRAMES.
    glass_door_min_frames: int = 1

    # --- Checkout dwell time ---
    # A single customer transaction at a counter zone. Sessions shorter
    # than min_dwell are dropped as pass-throughs (staff walking past,
    # tracker glitches); sessions longer than max_dwell are dropped as
    # not-a-transaction (staff working at the counter for a whole
    # shift — that's `staff_present`, not a checkout).
    # alert_minutes is the live-session ATTENTION-alert threshold.
    # Env: CHECKOUT_MIN_DWELL_SECONDS / CHECKOUT_MAX_DWELL_SECONDS /
    #      CHECKOUT_ALERT_MINUTES.
    checkout_min_dwell_seconds: int = 30
    checkout_max_dwell_seconds: int = 900
    checkout_alert_minutes:     int = 3

    # --- After-hours intrusion snapshot filmstrip ---
    # One intrusion alert per closed store with a person present; up to
    # `after_hours_max_snapshots` snapshots, the first immediately then one
    # every `after_hours_snapshot_interval_min` minutes, from the best
    # available store camera. See app/tasks/alerting.after_hours_intrusion_check.
    after_hours_snapshot_enabled: bool = True
    after_hours_snapshot_interval_min: int = 5
    after_hours_max_snapshots: int = 6
    # A person counts as "still present" if an intrusion event landed within
    # this many seconds (the detector re-fires roughly every 30s per zone).
    after_hours_present_grace_sec: int = 150

    # --- Rolling recording system (recorder service) ---
    # Records KEY cameras (counter / entry_exit / staff_zone) on substream
    # during business hours in rolling windows; each window is deleted at the
    # next transition. See app/tasks/recorder.py.
    recording_enabled: bool = True
    recording_business_start_eat: int = 9    # 09:00 EAT
    recording_business_end_eat: int = 20     # 20:00 EAT
    recording_max_used_gb_warning: int = 550
    recording_max_used_gb_critical: int = 580
    recording_alert_clip_retention_hours: int = 48

    # --- Storage paths (inside container) ---
    recordings_dir: str = "/data/recordings"
    models_dir: str = "/data/models"
    datasets_dir: str = "/data/datasets"
    thumbnails_dir: str = "/data/thumbnails"

    # --- Derived helpers ---
    @property
    def database_url(self) -> str:
        """SQLAlchemy URL.

        Honours DATABASE_URL when set (any SQLAlchemy URL — sqlite, RDS,
        managed Postgres). Otherwise composes one from POSTGRES_* fields
        using the psycopg v3 driver.
        """
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cache the Settings object so we don't re-parse env on every import."""
    return Settings()


# Convenience singleton — most modules import this directly.
settings = get_settings()
