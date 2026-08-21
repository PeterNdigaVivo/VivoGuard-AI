"""FastAPI application entrypoint.

Mounts the auth, cameras, NVR, detection-config, alerts, training, system
routers, plus the WebSocket endpoints. On startup we ensure a bootstrap
admin user exists.
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import SessionLocal
from app.utils.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: configure logging, run pending alembic migrations,
    ensure bootstrap admin, prep stores."""
    configure_logging("DEBUG" if settings.app_debug else "INFO")

    # Auto-run alembic migrations. Hand-running 'alembic upgrade head'
    # in PowerShell has caused repeated drift where the alembic_version
    # table is at head but the actual DDL never executed. Letting the
    # API container do it on every boot makes drift self-heal.
    try:
        from alembic.config import Config
        from alembic import command
        import os as _os
        # alembic.ini lives at /app/alembic.ini in the container image.
        cfg_path = "/app/alembic.ini" if _os.path.exists("/app/alembic.ini") else "alembic.ini"
        cfg = Config(cfg_path)
        command.upgrade(cfg, "head")
        import logging
        logging.getLogger(__name__).info("alembic upgrade head: complete")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("alembic upgrade skipped: %s", e)

    # Bootstrap admin (no-op once a user exists).
    from app.auth.routes import ensure_bootstrap_admin
    with SessionLocal() as db:
        try:
            ensure_bootstrap_admin(db, settings.bootstrap_admin_email, settings.bootstrap_admin_password)
        except Exception as e:
            # DB may not be migrated yet; don't crash the API server.
            import logging
            logging.getLogger(__name__).warning("bootstrap admin skipped: %s", e)

    # Spawn the alert engine notifier loop as a background asyncio task.
    import asyncio
    from app.alerts.engine import run_engine_forever
    engine_task = asyncio.create_task(run_engine_forever())

    yield

    engine_task.cancel()
    try:
        await engine_task
    except (asyncio.CancelledError, Exception):
        pass


app = FastAPI(
    title="VivoGuard AI",
    version="0.1.0",
    lifespan=lifespan,
    # All routes get a /api prefix when behind nginx; the proxy strips /api.
)

# CORS — frontend served from same origin via nginx, but allow localhost
# during dev (vite dev server).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_debug else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routers (filled in across the build) -----------------------------
from app.auth.routes import router as auth_router          # noqa: E402
from app.api.cameras  import router as cameras_router      # noqa: E402
from app.api.nvr      import router as nvr_router          # noqa: E402

app.include_router(auth_router)
app.include_router(cameras_router)
app.include_router(nvr_router)

# Auto-discovery for all optional routers. Earlier we swallowed
# ImportError silently — which masked real errors and made endpoints
# 404 mysteriously. Now we log the precise reason if a router fails
# to load. Operators can see the missing piece in
# `docker compose logs api`.
import logging as _logging
_router_log = _logging.getLogger("vivoguard.routers")

_OPTIONAL_ROUTERS = [
    ("app.api.users",              ["router"]),
    ("app.api.stores",             ["router"]),
    ("app.api.analytics",          ["router"]),
    ("app.api.stockroom",          ["router"]),
    ("app.api.search",             ["router"]),
    ("app.api.scheduled_reports",  ["router"]),
    ("app.api.detector_catalog",   ["router"]),
    ("app.api.detection_config",   ["router"]),
    ("app.api.zones",              ["router", "catalog_router"]),
    ("app.api.alerts",             ["router"]),
    ("app.api.labels",             ["router"]),
    ("app.api.training",           ["router"]),
    ("app.api.system",             ["router"]),
    ("app.api.system_health",      ["router"]),
    ("app.api.websockets",         ["router"]),
    ("app.api.agents",             ["router"]),
    ("app.api.operations",         ["router"]),
]
for _module_name, _attrs in _OPTIONAL_ROUTERS:
    try:
        _mod = __import__(_module_name, fromlist=_attrs)
        for _attr in _attrs:
            app.include_router(getattr(_mod, _attr))
        _router_log.info("loaded router: %s (%s)", _module_name, ",".join(_attrs))
    except Exception as _e:
        # Catch broadly — not just ImportError. The previous narrow
        # except hid SyntaxError, AttributeError, and missing-table
        # errors at startup, silently 404-ing the affected endpoints.
        _router_log.exception("FAILED to load router %s: %s", _module_name, _e)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe used by Docker healthcheck and load balancers."""
    return {"status": "ok"}
