"""FastAPI application entrypoint.

Filled in across steps 3, 4, 7, 8, 9. This stub exists so the project tree
is importable from step 1 onward.
"""
from fastapi import FastAPI

# The real app is assembled later (auth router, cameras router, training
# router, websockets, lifespan hooks for stream manager + Redis, etc).
app = FastAPI(title="VivoGuard AI", version="0.1.0-step1")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe used by Docker healthcheck and load balancers."""
    return {"status": "ok"}
