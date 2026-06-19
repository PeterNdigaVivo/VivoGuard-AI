"""Inference Celery tasks.

Two tasks here:

  inference.supervise_all  — short, fires every 30s via Celery beat.
                             Lists every `ai_enabled` camera and enqueues
                             a long-running `run_camera_inference` task
                             for any camera that isn't already covered.
                             Uses a Redis lock per camera so concurrent
                             beat ticks (and worker restarts) don't
                             double-queue.

  inference.run_camera     — long-running task; pulls frames from the
                             Redis frame buffer for one camera and runs
                             the full detector chain. Returns after
                             `max_seconds` so the supervisor re-queues
                             it cleanly (lock TTL > max_seconds so the
                             camera stays covered across the handoff).
"""
from __future__ import annotations
import logging
import time

import redis

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


# ---- Tunables -----------------------------------------------------------
# Each per-camera task runs this long then exits. Keep it well under the
# supervisor lock TTL so we don't end up with double-coverage.
RUN_SECONDS    = settings.inference_run_seconds   # default 120s, env-overridable
LOCK_TTL       = RUN_SECONDS + 60                  # outlives RUN_SECONDS by 1 min
                                                    # (crashed-worker safety net;
                                                    #  normal exit releases the lock)
LOCK_KEY_FMT   = "vg:inference-lock:{camera_id}"
HB_KEY_FMT     = "vg:inference-hb:{camera_id}"     # heartbeat (debug only)


def _redis() -> redis.Redis:
    return redis.from_url(settings.redis_url)


# -------------------------------------------------------------------------
# Supervisor (fired by Celery beat every 30s)
# -------------------------------------------------------------------------

@celery_app.task(name="inference.supervise_all", ignore_result=True)
def supervise_all() -> None:
    """Enqueue an inference task for every active camera that isn't
    currently covered. Cheap — runs in ~milliseconds; the heavy lifting
    is done by the per-camera tasks it enqueues."""
    from app.database import SessionLocal
    from app.models import Camera

    r = _redis()
    enqueued: list[int] = []
    skipped:  list[int] = []
    with SessionLocal() as db:
        cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
    for cam in cams:
        lock = LOCK_KEY_FMT.format(camera_id=cam.id)
        # SET NX with TTL — atomic acquire. Returns truthy only when we
        # actually took the lock.
        if r.set(lock, "1", ex=LOCK_TTL, nx=True):
            run_camera_inference.delay(cam.id, max_seconds=RUN_SECONDS)
            enqueued.append(cam.id)
        else:
            skipped.append(cam.id)
    log.info("inference.supervise_all: enqueued=%s, already_running=%s",
             enqueued, skipped)


# -------------------------------------------------------------------------
# Per-camera long-running inference
# -------------------------------------------------------------------------

@celery_app.task(name="inference.run_camera", bind=True, ignore_result=True)
def run_camera_inference(self, camera_id: int, *, max_seconds: int = RUN_SECONDS) -> None:
    """Pull frames + run detectors for one camera. Always releases its
    Redis lock on exit (normal or exceptional)."""
    from app.ai.inference_worker import run_for_camera
    r = _redis()
    lock = LOCK_KEY_FMT.format(camera_id=camera_id)
    hb   = HB_KEY_FMT.format(camera_id=camera_id)
    log.info("inference.run_camera camera=%s max_seconds=%s start", camera_id, max_seconds)
    started = time.time()
    try:
        # Heartbeat key for the System Health page / debugging.
        r.set(hb, int(started), ex=max(60, max_seconds + 60))
        run_for_camera(camera_id, max_seconds=max_seconds)
    except Exception as e:
        log.exception("inference.run_camera camera=%s failed: %s", camera_id, e)
        # Don't auto-retry the whole task — the supervisor will re-enqueue
        # on the next 30-second tick once the lock expires.
    finally:
        # Release the lock so the supervisor can re-enqueue immediately
        # rather than waiting for TTL to elapse.
        try:
            r.delete(lock)
        except Exception:
            pass
        log.info("inference.run_camera camera=%s exited after %.1fs",
                 camera_id, time.time() - started)
