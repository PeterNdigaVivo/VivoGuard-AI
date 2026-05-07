"""Application configuration loaded from environment variables.

Single source of truth for every tunable. Imported via `from app.config
import settings` everywhere else; never read os.environ directly.
"""
from functools import lru_cache
from pydantic import Field
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
    app_timezone: str = "UTC"

    # --- Auth ---
    jwt_secret: str = Field(default="dev-only-change-me", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60
    jwt_refresh_ttl_days: int = 30
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "change-me-now"

    # --- Postgres ---
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
    use_gpu: bool = False
    cuda_visible_devices: str = "0"
    default_model: str = "yolov8n.pt"
    inference_fps_default: int = 5

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
    webhook_url: str = ""
    webhook_auth_header: str = ""

    # --- WAN behaviour ---
    ddns_refresh_interval_seconds: int = 300
    wan_reconnect_initial_delay_seconds: int = 5
    wan_reconnect_max_delay_seconds: int = 300
    wan_offline_alert_threshold_seconds: int = 120

    # --- Storage paths (inside container) ---
    recordings_dir: str = "/data/recordings"
    models_dir: str = "/data/models"
    datasets_dir: str = "/data/datasets"
    thumbnails_dir: str = "/data/thumbnails"

    # --- Derived helpers ---
    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for psycopg v3 driver."""
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
