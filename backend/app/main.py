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
    """Startup: configure logging, ensure bootstrap admin, prep stores."""
    configure_logging("DEBUG" if settings.app_debug else "INFO")

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

# Detection config, zones, alerts, training, system, websockets routers
# are included from steps 7–9 and 14.
# Retail extension routers (commit 0+).
try:
    from app.api.stores import router as stores_router               # noqa: E402
    app.include_router(stores_router)
except ImportError:
    pass
try:
    from app.api.analytics import router as analytics_router         # noqa: E402
    app.include_router(analytics_router)
except ImportError:
    pass

try:
    from app.api.detection_config import router as detection_router  # noqa: E402
    app.include_router(detection_router)
except ImportError:
    pass
try:
    from app.api.zones import router as zones_router                 # noqa: E402
    app.include_router(zones_router)
except ImportError:
    pass
try:
    from app.api.alerts import router as alerts_router               # noqa: E402
    app.include_router(alerts_router)
except ImportError:
    pass
try:
    from app.api.training import router as training_router           # noqa: E402
    app.include_router(training_router)
except ImportError:
    pass
try:
    from app.api.system import router as system_router               # noqa: E402
    app.include_router(system_router)
except ImportError:
    pass
try:
    from app.api.websockets import router as ws_router               # noqa: E402
    app.include_router(ws_router)
except ImportError:
    pass


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe used by Docker healthcheck and load balancers."""
    return {"status": "ok"}
