"""Celery entry point for AI alert verification (annotate, never hide).

alerts.verify runs on the `alerts` queue, enqueued fire-and-forget
right after an alert row is committed. It runs for EVERY alert,
including rows flagged review_only by other mechanisms, and only ever
writes the ai_* annotation columns via the service.

Concurrency: a Redis counting semaphore of settings.verifier_parallel
(default 4) so an alert burst cannot open dozens of API calls at
once. Excess callers WAIT (bounded, with a TTL on the counter so a
leaked slot self-heals); they are never dropped. Network retry lives
inside the service (one retry after 5s).
"""
from __future__ import annotations

import logging
import random
import time

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

_SEM_KEY = "vg:verifier:active"
_SEM_TTL_S = 180        # counter self-heals if a worker dies mid-call
_WAIT_MAX_S = 300       # bounded wait: a leaked counter must not wedge
                        # the queue slot forever; after this we proceed


def _acquire_slot(r, limit: int) -> bool:
    """Blocking acquire on the counting semaphore. Returns True when a
    slot was taken (caller must release). On Redis failure or wait
    timeout we proceed WITHOUT a slot rather than dropping the task."""
    deadline = time.time() + _WAIT_MAX_S
    while True:
        try:
            n = int(r.incr(_SEM_KEY))
            r.expire(_SEM_KEY, _SEM_TTL_S)
            if n <= limit:
                return True
            r.decr(_SEM_KEY)
        except Exception as e:
            log.warning("verifier semaphore unavailable (%s) - proceeding",
                        e)
            return False
        if time.time() >= deadline:
            log.warning("verifier semaphore wait exceeded %ss - proceeding",
                        _WAIT_MAX_S)
            return False
        time.sleep(0.5 + random.random() * 0.5)


def _release_slot(r) -> None:
    try:
        if int(r.decr(_SEM_KEY)) < 0:
            r.delete(_SEM_KEY)
    except Exception:
        pass


@celery_app.task(name="alerts.verify", ignore_result=True)
def verify_alert(alert_id: int, force: bool = False) -> None:
    """Verify one alert. force=True (the admin re-run endpoint) skips
    the verifier_enabled gate but not the semaphore."""
    if not force and not bool(getattr(settings, "verifier_enabled", False)):
        return
    r = None
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True,
                            socket_timeout=3)
    except Exception:
        r = None
    acquired = False
    if r is not None:
        limit = max(1, int(getattr(settings, "verifier_parallel", 4)))
        acquired = _acquire_slot(r, limit)
    try:
        from app.services.alert_verifier import verify
        verify(alert_id)
    finally:
        if acquired and r is not None:
            _release_slot(r)
