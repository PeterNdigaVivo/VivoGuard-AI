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
    checkout_alert_minutes:     int = 8

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
