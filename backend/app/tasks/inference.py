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
CRITICAL_RUN_SECONDS = settings.inference_critical_slice_seconds
CRITICAL_GAP_SLA_SECONDS = settings.inference_critical_gap_sla_seconds
CRITICAL_REQUEUE_SECONDS = min(
    settings.inference_critical_requeue_seconds,
    CRITICAL_GAP_SLA_SECONDS,
)
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
LAST_RUN_KEY_FMT = "vg:inference:last-run:{camera_id}"
HEALTH_KEY     = "vg:inference:health"             # supervisor breadcrumb
LOCK_KEY_PREFIX = "vg:inference-lock:"
REDIS_PRIORITY_SEP = "\x06\x16"

# These risks need prompt observation even while the CPU host is rotating
# through slower dwell, analytics and merchandising work. ``person`` is
# intentionally absent: including it would classify almost the whole fleet as
# critical and defeat the fast lane.
CRITICAL_DETECTION_TYPES = frozenset({
    "entry_exit", "shutter", "intrusion", "trespass", "tripwire",
    "tailgating", "fall", "fight", "weapon", "weapon_brandished",
    "fire", "smoke", "staff_zone", "stockroom_access",
})

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


def _camera_is_latency_critical(camera) -> bool:
    """Return whether a camera protects a time-critical retail risk."""
    if any(
        bool(config.enabled)
        and str(config.detection_type) in CRITICAL_DETECTION_TYPES
        for config in (camera.detection_configs or [])
    ):
        return True
    return any(
        not bool(zone.suppressed)
        and bool(
            CRITICAL_DETECTION_TYPES.intersection(
                str(value) for value in (zone.detection_types_json or [])
            )
        )
        for zone in (camera.zones or [])
    )


def _task_profile(camera) -> tuple[int, int]:
    """Return ``(slice_seconds, priority)`` for one camera reservation."""
    if _camera_is_latency_critical(camera):
        # Kombu's Redis transport checks the priority-0 list before priority-9
        # (RabbitMQ uses the inverse convention). This service uses Redis.
        return int(CRITICAL_RUN_SECONDS), 0
    return int(RUN_SECONDS), 9


def _critical_due_from_timestamp(value, *, now: float) -> bool:
    """Fail open unless the camera completed inference inside its cooldown."""
    if isinstance(value, bytes):
        value = value.decode()
    try:
        age = max(0.0, float(now) - float(value))
    except (TypeError, ValueError):
        return True
    return age >= CRITICAL_REQUEUE_SECONDS


def _last_run_timestamps(
    r: redis.Redis,
    camera_ids: list[int],
) -> dict[int, float | None]:
    """Read completed-run timestamps in one Redis round trip.

    Missing or malformed values deliberately become ``None`` so scheduling
    fails open: a camera without trustworthy coverage history is treated as
    the oldest and therefore receives service first.
    """
    if not camera_ids:
        return {}
    try:
        pipe = r.pipeline(transaction=False)
        for camera_id in camera_ids:
            pipe.get(LAST_RUN_KEY_FMT.format(camera_id=camera_id))
        values = pipe.execute()
    except Exception as exc:
        log.warning(
            "supervise_all: last-run batch read failed; treating all "
            "critical cameras as overdue: %s", exc,
        )
        return {camera_id: None for camera_id in camera_ids}

    result: dict[int, float | None] = {}
    for camera_id, value in zip(camera_ids, values):
        if isinstance(value, bytes):
            value = value.decode()
        try:
            result[camera_id] = float(value) if value is not None else None
        except (TypeError, ValueError):
            result[camera_id] = None
    return result


def _schedule_order(
    cameras: list,
    last_run_by_camera: dict[int, float | None] | None = None,
) -> list:
    """Publish critical work first, oldest successful analysis first.

    Stable camera/DB ordering repeatedly favoured low IDs whenever CPU
    capacity was saturated. Ordering the critical fast lane by its actual
    last completed run prevents that starvation pattern. Ordinary cameras
    retain their existing stable order.
    """
    last_runs = last_run_by_camera or {}

    def priority(camera) -> tuple[int, float]:
        if not _camera_is_latency_critical(camera):
            return (1, 0.0)
        timestamp = last_runs.get(int(camera.id))
        return (0, float("-inf") if timestamp is None else float(timestamp))

    return sorted(cameras, key=priority)


def _critical_gap_health(
    r: redis.Redis,
    camera_ids: list[int],
    *,
    now: float | None = None,
) -> dict:
    """Measure critical-camera scheduling gaps from actual task starts."""
    now = time.time() if now is None else float(now)
    if not camera_ids:
        return {
            "critical_cameras_total": 0,
            "critical_cameras_overdue": 0,
            "critical_camera_ids_overdue": [],
            "critical_cameras_never_started": 0,
            "critical_max_gap_seconds": 0,
            "critical_gap_sla_seconds": int(CRITICAL_GAP_SLA_SECONDS),
        }
    try:
        pipe = r.pipeline(transaction=False)
        for camera_id in camera_ids:
            pipe.get(LAST_RUN_KEY_FMT.format(camera_id=camera_id))
        values = pipe.execute()
        ages: dict[int, float | None] = {}
        for camera_id, value in zip(camera_ids, values):
            if isinstance(value, bytes):
                value = value.decode()
            try:
                ages[camera_id] = max(0.0, now - float(value)) if value else None
            except (TypeError, ValueError):
                ages[camera_id] = None
        overdue = sorted(
            camera_id for camera_id, age in ages.items()
            if age is None or age > CRITICAL_GAP_SLA_SECONDS
        )
        measured = [age for age in ages.values() if age is not None]
        return {
            "critical_cameras_total": len(camera_ids),
            "critical_cameras_overdue": len(overdue),
            "critical_camera_ids_overdue": overdue,
            "critical_cameras_never_started": sum(
                age is None for age in ages.values()
            ),
            "critical_max_gap_seconds": (
                round(max(measured), 1) if measured else None
            ),
            "critical_gap_sla_seconds": int(CRITICAL_GAP_SLA_SECONDS),
        }
    except Exception as exc:
        log.warning("supervise_all: critical-gap telemetry failed: %s", exc)
        return {
            "critical_cameras_total": len(camera_ids),
            "critical_cameras_overdue": None,
            "critical_camera_ids_overdue": None,
            "critical_cameras_never_started": None,
            "critical_max_gap_seconds": None,
            "critical_gap_sla_seconds": int(CRITICAL_GAP_SLA_SECONDS),
        }


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
    queues = _inference_queue_names()
    if not camera_ids:
        return {
            "cameras_reserved": 0,
            "cameras_actively_inferencing": 0,
            "cameras_waiting_for_worker": 0,
            "inference_queue_depth": 0,
            "inference_queue_depth_by_shard": {queue: 0 for queue in queues},
            "inference_shards": {
                queue: {
                    "cameras": 0,
                    "reserved": 0,
                    "active": 0,
                    "queue_depth": 0,
                }
                for queue in queues
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
        # Kombu's Redis priority emulation stores priority 0 in the base list
        # and priorities 1..9 in suffixed lists. Counting only the base queue
        # hides ordinary pending tasks as soon as the fast lane is enabled.
        queue_depths = {
            queue: sum(
                int(r.llen(
                    queue if priority == 0
                    else f"{queue}{REDIS_PRIORITY_SEP}{priority}"
                ))
                for priority in range(10)
            )
            for queue in queues
        }
        shard_health = {
            queue: {
                "cameras": 0,
                "reserved": 0,
                "active": 0,
                "queue_depth": queue_depths[queue],
            }
            for queue in queues
        }
        for index, camera_id in enumerate(camera_ids):
            queue = _inference_queue(camera_id)
            shard_health[queue]["cameras"] += 1
            shard_health[queue]["reserved"] += int(bool(flags[index]))
            shard_health[queue]["active"] += int(bool(flags[split + index]))
        return {
            "cameras_reserved": reserved,
            "cameras_actively_inferencing": active,
            "cameras_waiting_for_worker": max(0, reserved - active),
            "inference_queue_depth": sum(queue_depths.values()),
            "inference_queue_depth_by_shard": queue_depths,
            "inference_shards": shard_health,
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
            "inference_shards": None,
            "estimated_full_rotation_seconds": None,
        }


def _write_health(r: redis.Redis, *,
                  cameras_total: int, cameras_fresh: int,
                  cameras_enqueued: int,
                  cameras_already_running: int, stale_cleared: int,
                  fresh_camera_ids: list[int] | None = None,
                  reservation_health: dict | None = None) -> None:
    """Heartbeat for the inference pipeline. The
    alerting.inference_pipeline_health_check beat task fires URGENT
    when this key ages past 10 min."""
    try:
        payload = {
            "last_run_ts":             int(time.time()),
            "cameras_total":           cameras_total,
            "cameras_fresh":           cameras_fresh,
            "fresh_camera_ids":        sorted(fresh_camera_ids or []),
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
    from sqlalchemy.orm import selectinload

    r = _redis()
    stale_cleared = _sweep_stale_locks(r)
    enqueued: list[int] = []
    skipped:  list[int] = []
    cooling_down: list[int] = []
    with SessionLocal() as db:
        cams = (
            db.query(Camera)
            .options(
                selectinload(Camera.detection_configs),
                selectinload(Camera.zones),
            )
            .filter(Camera.ai_enabled == True)  # noqa: E712
            .all()
        )
    fresh_ids = _camera_ids_with_fresh_frames(r, [int(cam.id) for cam in cams])
    schedulable = [cam for cam in cams if int(cam.id) in fresh_ids]
    critical_ids = [
        int(cam.id) for cam in schedulable if _camera_is_latency_critical(cam)
    ]
    critical_last_runs = _last_run_timestamps(r, critical_ids)
    schedule_now = time.time()
    # Workers may be idle while this loop publishes. Priority only reorders
    # messages already in Redis, so publishing ordinary tasks first can let
    # them start before a later critical message exists. Critical-first
    # publication closes that race. Within the fast lane, the camera with the
    # oldest completed run is published first to prevent CPU-era starvation.
    for cam in _schedule_order(schedulable, critical_last_runs):
        if _camera_is_latency_critical(cam):
            last_run = critical_last_runs.get(int(cam.id))
            if not _critical_due_from_timestamp(last_run, now=schedule_now):
                cooling_down.append(int(cam.id))
                continue
        lock = LOCK_KEY_FMT.format(camera_id=cam.id)
        # SET NX with TTL — atomic acquire. Value is a placeholder until
        # we know the real task_id; stamped below.
        placeholder = f"pending:{int(time.time())}"
        if r.set(lock, placeholder, ex=PENDING_LOCK_TTL, nx=True):
            try:
                max_seconds, priority = _task_profile(cam)
                ar = run_camera_inference.apply_async(
                    args=[cam.id],
                    kwargs={"max_seconds": max_seconds},
                    queue=_inference_queue(int(cam.id)),
                    priority=priority,
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
             "already_running=%s, critical_cooldown=%s, "
             "stale_locks_cleared=%d",
             len(cams), len(schedulable), enqueued, skipped, cooling_down,
             stale_cleared)
    reservation_health = _reservation_health(
        r, [int(cam.id) for cam in schedulable],
    )
    reservation_health.update(_critical_gap_health(r, critical_ids))
    _write_health(r,
                  cameras_total=len(cams),
                  cameras_fresh=len(schedulable),
                  cameras_enqueued=len(enqueued),
                  cameras_already_running=len(skipped),
                  stale_cleared=stale_cleared,
                  fresh_camera_ids=[int(cam.id) for cam in schedulable],
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
        processed_frames = run_for_camera(camera_id, max_seconds=max_seconds)
        # Record only completed inference work. A task that merely started but
        # found a stale/invalid frame must not make the coverage SLA look green.
        if processed_frames > 0:
            r.set(
                LAST_RUN_KEY_FMT.format(camera_id=camera_id),
                str(int(time.time())),
                ex=24 * 60 * 60,
            )
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
