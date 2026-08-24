"""Inference Celery tasks.

Two tasks here:

  inference.supervise_all  — short, fires every 30s via Celery beat.
                             Lists every `ai_enabled` camera, sweeps
                             stale locks, then enqueues a long-running
                             `run_camera_inference` task for any
                             camera that isn't already covered. The
                             stale-lock sweep is the self-healing
                             primitive that lets the system recover
                             from worker crashes / OOM / SIGKILL
                             without manual intervention — the only
                             remaining downtime is bounded by
                             LOCK_TTL (150s).

  inference.run_camera     — long-running task; pulls frames from the
                             Redis frame buffer for one camera and runs
                             the full detector chain. ALWAYS releases
                             its lock on exit via try/finally; the
                             stale-lock sweep covers the cases where
                             finally itself can't run (SIGKILL etc.).
"""
from __future__ import annotations
import json
import logging
import math
import time

import redis

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


# ---- Tunables -----------------------------------------------------------
# Each per-camera task runs this long then exits. Keep it well under the
# supervisor lock TTL so we don't end up with double-coverage.
RUN_SECONDS    = settings.inference_run_seconds   # default 120s, env-overridable
# Tightened from RUN_SECONDS+60 to RUN_SECONDS+30. A lock that outlives
# its task by >30s is stale by definition; the self-healing sweep clears
# it before TTL expiry anyway, this just bounds the worst case when
# Redis-side TTL is the only remaining safety net (e.g. supervisor itself
# down).
LOCK_TTL       = RUN_SECONDS + 30                  # 150s
# A reservation is created before publishing a task. With a large live fleet,
# a legitimately queued task can wait several minutes
# before it starts.  Giving pending reservations an hour prevents Beat from
# publishing the same camera every LOCK_TTL while still bounding recovery if
# the broker loses a task entirely.
PENDING_LOCK_TTL = max(3600, RUN_SECONDS * 32)
# Heartbeat TTL must comfortably exceed LOCK_TTL so a missing-hb on a
# still-live lock is unambiguously "task is dead", never a race.
HB_TTL         = LOCK_TTL + 30                     # 180s
LOCK_KEY_FMT   = "vg:inference-lock:{camera_id}"
HB_KEY_FMT     = "vg:inference-hb:{camera_id}"     # set once at task start
HEALTH_KEY     = "vg:inference:health"             # supervisor breadcrumb
LOCK_KEY_PREFIX = "vg:inference-lock:"

_CLAIM_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('expire', KEYS[1], tonumber(ARGV[2]))
  redis.call('set', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[3]))
  return 1
end
return 0
"""

_RELEASE_LUA = """
local released = 0
if redis.call('get', KEYS[1]) == ARGV[1] then
  released = released + redis.call('del', KEYS[1])
end
if redis.call('get', KEYS[2]) == ARGV[1] then
  released = released + redis.call('del', KEYS[2])
end
return released
"""

# Sweep safety — we only clear a lock when hb is missing AND the lock
# has ≤ this many seconds of TTL left. The lock-TTL grace prevents
# racing a still-starting task whose hb hasn't landed yet.
SWEEP_TTL_THRESHOLD_S = 30


def _redis() -> redis.Redis:
    return redis.from_url(settings.redis_url)


def _inference_queue(camera_id: int, shard_count: int | None = None) -> str:
    """Return the stable worker queue for a camera.

    The one-shard default deliberately retains the historical ``inference``
    queue, making this feature a no-op until operators provision all shard
    consumers. Modulo assignment is deterministic across supervisor restarts
    and does not require mutable routing state.
    """
    count = int(
        settings.inference_shard_count if shard_count is None else shard_count
    )
    if count < 1:
        raise ValueError("INFERENCE_SHARD_COUNT must be at least 1")
    return "inference" if count == 1 else f"inference.{int(camera_id) % count}"


def _inference_queue_names(shard_count: int | None = None) -> list[str]:
    count = int(
        settings.inference_shard_count if shard_count is None else shard_count
    )
    if count < 1:
        raise ValueError("INFERENCE_SHARD_COUNT must be at least 1")
    return ["inference"] if count == 1 else [
        f"inference.{index}" for index in range(count)
    ]


def _camera_ids_with_fresh_frames(
    r: redis.Redis, camera_ids: list[int],
) -> set[int]:
    """Return cameras whose raw-frame key currently exists.

    A camera without ``vg:frame:{id}`` has no pixels to analyse. Scheduling
    its two-minute task only occupies a scarce CPU worker while it polls an
    empty buffer, delaying every live camera behind it. The streamer gives
    raw-frame keys a 30-second TTL, so this is also a precise freshness gate.

    Redis inspection fails open: if the batch check itself fails, return all
    IDs. A cache fault must degrade efficiency, never silently suspend the
    inference fleet.
    """
    if not camera_ids:
        return set()
    try:
        pipe = r.pipeline(transaction=False)
        for camera_id in camera_ids:
            pipe.exists(f"vg:frame:{camera_id}")
        present = pipe.execute()
        return {
            camera_id
            for camera_id, exists in zip(camera_ids, present)
            if bool(exists)
        }
    except Exception as exc:
        log.warning(
            "supervise_all: frame-freshness check failed; scheduling all "
            "AI-enabled cameras: %s", exc,
        )
        return set(camera_ids)


def _claim_reserved_task(
    r: redis.Redis, *, lock: str, heartbeat: str, task_id: str,
) -> bool:
    """Atomically convert this task's pending reservation to an active lock."""
    return bool(r.eval(
        _CLAIM_LUA, 2, lock, heartbeat, task_id, LOCK_TTL, HB_TTL,
    ))


def _release_owned_task(
    r: redis.Redis, *, lock: str, heartbeat: str, task_id: str,
) -> int:
    """Release only keys owned by this task, never a newer task's lease."""
    return int(r.eval(_RELEASE_LUA, 2, lock, heartbeat, task_id))


def _sweep_stale_locks(r: redis.Redis) -> int:
    """Self-healing sweep. A lock is stale when its heartbeat key is
    missing AND the lock has ≤ SWEEP_TTL_THRESHOLD_S seconds remaining.

    Why both conditions are required for safety (no double-inference):
      • HB_TTL (180s) > LOCK_TTL (150s) > RUN_SECONDS (120s). A live
        worker's hb cannot expire mid-run.
      • If hb is missing, the task is dead OR never started. Cannot be
        running.
      • The lock-TTL grace (≤30s) avoids deleting a lock during the
        first ~120s of its life when a real task could theoretically
        still be starting up between `.set(lock)` and `r.set(hb)`.
        Worst case the sweep waits one extra tick (30s) before clearing
        — bounded latency, zero risk.

    Returns the count of locks cleared (also surfaced in the health
    breadcrumb for the URGENT alerter).
    """
    cleared = 0
    try:
        for key in r.scan_iter(match=f"{LOCK_KEY_PREFIX}*", count=200):
            try:
                key_s = key.decode() if isinstance(key, bytes) else key
                cam_id = int(key_s.rsplit(":", 1)[-1])
            except Exception:
                continue
            hb_present = bool(r.get(HB_KEY_FMT.format(camera_id=cam_id)))
            if hb_present:
                continue                # live worker — leave alone
            ttl = r.ttl(key)            # remaining lock TTL in seconds
            if ttl is None or ttl < 0:
                continue                # already expiring / Redis quirk
            if ttl <= SWEEP_TTL_THRESHOLD_S:
                if r.delete(key):
                    cleared += 1
                    log.warning(
                        "supervise_all: cleared stale lock cam=%s "
                        "(no hb, ttl was %ss)", cam_id, ttl)
    except Exception as e:
        # Sweep failure must not kill the supervisor — the normal
        # SET NX path still works without the heal.
        log.warning("supervise_all: stale-lock sweep failed: %s", e)
    return cleared


def _reservation_health(r: redis.Redis, camera_ids: list[int]) -> dict:
    """Report active versus queued camera reservations without side effects."""
    if not camera_ids:
        return {
            "cameras_reserved": 0,
            "cameras_actively_inferencing": 0,
            "cameras_waiting_for_worker": 0,
            "inference_queue_depth": 0,
            "inference_queue_depth_by_shard": {
                queue: 0 for queue in _inference_queue_names()
            },
            "estimated_full_rotation_seconds": 0,
        }
    try:
        pipe = r.pipeline(transaction=False)
        for camera_id in camera_ids:
            pipe.exists(LOCK_KEY_FMT.format(camera_id=camera_id))
        for camera_id in camera_ids:
            pipe.exists(HB_KEY_FMT.format(camera_id=camera_id))
        flags = pipe.execute()
        split = len(camera_ids)
        reserved = sum(bool(value) for value in flags[:split])
        active = sum(bool(value) for value in flags[split:])
        queue_depths = {
            queue: int(r.llen(queue)) for queue in _inference_queue_names()
        }
        return {
            "cameras_reserved": reserved,
            "cameras_actively_inferencing": active,
            "cameras_waiting_for_worker": max(0, reserved - active),
            "inference_queue_depth": sum(queue_depths.values()),
            "inference_queue_depth_by_shard": queue_depths,
            "estimated_full_rotation_seconds": (
                math.ceil(len(camera_ids) / active) * RUN_SECONDS
                if active else None
            ),
        }
    except Exception as exc:
        log.warning("supervise_all: reservation telemetry failed: %s", exc)
        return {
            "cameras_reserved": None,
            "cameras_actively_inferencing": None,
            "cameras_waiting_for_worker": None,
            "inference_queue_depth": None,
            "inference_queue_depth_by_shard": None,
            "estimated_full_rotation_seconds": None,
        }


def _write_health(r: redis.Redis, *,
                  cameras_total: int, cameras_fresh: int,
                  cameras_enqueued: int,
                  cameras_already_running: int, stale_cleared: int,
                  reservation_health: dict | None = None) -> None:
    """Heartbeat for the inference pipeline. The
    alerting.inference_pipeline_health_check beat task fires URGENT
    when this key ages past 10 min."""
    try:
        payload = {
            "last_run_ts":             int(time.time()),
            "cameras_total":           cameras_total,
            "cameras_fresh":           cameras_fresh,
            "cameras_without_frames":  max(0, cameras_total - cameras_fresh),
            "cameras_enqueued":        cameras_enqueued,
            "cameras_already_running": cameras_already_running,
            "stale_locks_cleared":     stale_cleared,
            "inference_shard_count":   int(settings.inference_shard_count),
        }
        payload.update(reservation_health or {})
        r.set(HEALTH_KEY, json.dumps(payload), ex=24 * 3600)
    except Exception as e:
        log.debug("supervise_all: health write failed: %s", e)


# -------------------------------------------------------------------------
# Supervisor (fired by Celery beat every 30s)
# -------------------------------------------------------------------------

@celery_app.task(name="inference.supervise_all", ignore_result=True)
def supervise_all() -> None:
    """Enqueue an inference task for every active camera that isn't
    currently covered. Cheap — runs in ~milliseconds; the heavy lifting
    is done by the per-camera tasks it enqueues.

    Self-healing: sweeps stale locks (worker crashed without releasing)
    BEFORE attempting the per-camera acquire. A camera whose lock was
    stale will be picked up on the same tick that clears it."""
    from app.database import SessionLocal
    from app.models import Camera

    r = _redis()
    stale_cleared = _sweep_stale_locks(r)
    enqueued: list[int] = []
    skipped:  list[int] = []
    with SessionLocal() as db:
        cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
    fresh_ids = _camera_ids_with_fresh_frames(r, [int(cam.id) for cam in cams])
    schedulable = [cam for cam in cams if int(cam.id) in fresh_ids]
    for cam in schedulable:
        lock = LOCK_KEY_FMT.format(camera_id=cam.id)
        # SET NX with TTL — atomic acquire. Value is a placeholder until
        # we know the real task_id; stamped below.
        placeholder = f"pending:{int(time.time())}"
        if r.set(lock, placeholder, ex=PENDING_LOCK_TTL, nx=True):
            try:
                ar = run_camera_inference.apply_async(
                    args=[cam.id],
                    kwargs={"max_seconds": RUN_SECONDS},
                    queue=_inference_queue(int(cam.id)),
                )
                # Stamp the reservation with the real task_id.  It remains a
                # long pending lease until that exact task atomically claims
                # it at worker start.
                r.set(lock, ar.id, ex=PENDING_LOCK_TTL)
                enqueued.append(cam.id)
            except Exception as e:
                # apply_async failed (broker hiccup). Release the lock
                # so the next tick re-acquires immediately rather than
                # waiting LOCK_TTL.
                log.exception("supervise_all: enqueue failed cam=%s: %s",
                              cam.id, e)
                try: r.delete(lock)
                except Exception: pass
        else:
            skipped.append(cam.id)
    log.info("inference.supervise_all: total=%d, fresh=%d, enqueued=%s, "
             "already_running=%s, stale_locks_cleared=%d",
             len(cams), len(schedulable), enqueued, skipped, stale_cleared)
    reservation_health = _reservation_health(
        r, [int(cam.id) for cam in schedulable],
    )
    _write_health(r,
                  cameras_total=len(cams),
                  cameras_fresh=len(schedulable),
                  cameras_enqueued=len(enqueued),
                  cameras_already_running=len(skipped),
                  stale_cleared=stale_cleared,
                  reservation_health=reservation_health)


# -------------------------------------------------------------------------
# Per-camera long-running inference
# -------------------------------------------------------------------------

@celery_app.task(name="inference.run_camera", bind=True, ignore_result=True)
def run_camera_inference(self, camera_id: int, *, max_seconds: int = RUN_SECONDS) -> None:
    """Pull frames + run detectors for one camera. Always releases its
    Redis lock on exit (normal or exceptional). The stale-lock sweep
    in supervise_all covers the cases where this `finally` can't run
    (SIGKILL, OOM-kill, container hard restart, C-level segfault)."""
    from app.ai.inference_worker import run_for_camera
    r = _redis()
    lock = LOCK_KEY_FMT.format(camera_id=camera_id)
    hb   = HB_KEY_FMT.format(camera_id=camera_id)
    task_id = str(self.request.id)
    log.info("inference.run_camera camera=%s max_seconds=%s start", camera_id, max_seconds)
    started = time.time()
    claimed = False
    try:
        # Old/duplicate broker messages must never run.  Only the exact task
        # id recorded by the supervisor may convert the pending reservation
        # into an active lock and heartbeat.
        claimed = _claim_reserved_task(
            r, lock=lock, heartbeat=hb, task_id=task_id,
        )
        if not claimed:
            log.warning(
                "inference.run_camera camera=%s task=%s skipped: "
                "reservation missing or owned by another task",
                camera_id, task_id,
            )
            return
        run_for_camera(camera_id, max_seconds=max_seconds)
    except Exception as e:
        log.exception("inference.run_camera camera=%s failed: %s", camera_id, e)
        # Don't auto-retry the whole task — the supervisor will re-enqueue
        # on the next 30-second tick once the lock is released below
        # (or, on SIGKILL, cleared by the sweep).
    finally:
        # A stale task must not delete a newer task's reservation/heartbeat.
        if claimed:
            try:
                _release_owned_task(
                    r, lock=lock, heartbeat=hb, task_id=task_id,
                )
            except Exception:
                pass
        log.info("inference.run_camera camera=%s exited after %.1fs",
                 camera_id, time.time() - started)
