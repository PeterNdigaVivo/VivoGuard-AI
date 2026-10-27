"""Redis-backed "this condition has held for N seconds" state.

Why this module exists
----------------------
``run_for_camera()`` builds a fresh ``DetectorRegistry`` on every inference
slice, and a slice is ~15 seconds. Every detector instance is therefore
reconstructed several times a minute and any timer kept in Python memory
resets before it can mature. A detector asking "have 6 people been here for
5 minutes?" can never answer yes: its clock is destroyed twenty times over
the course of that question.

``StaffPresenceDetector`` already works around this by persisting its
per-(camera, zone) state in Redis, which is exactly why "Counter Left
Unattended" fires correctly at a 10-minute threshold while ``crowd`` — with
the same shape of question — had produced zero events fleet-wide.

Aggregate only, never per-track
-------------------------------
State here is keyed by (camera, zone, condition) and records only *that* a
condition holds, never *who* satisfies it.

That restriction is deliberate and load-bearing. ``VivoGuardTracker`` sets
``self._next_id = 1`` in its constructor and holds no external state, so
track ids restart from 1 on every slice. A key like
``vg:...:{camera}:{track_id}`` would therefore hand one person's start
timestamp to a completely unrelated person on the next slice — the detector
would report confident, fabricated dwell times rather than simply failing.
Wrong numbers are worse than missing ones, so identity is kept out of this
module entirely.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_PREFIX = "vg:sustain"

# Fallback used only when Redis is unreachable. It still resets with the
# slice, so sustained conditions degrade to "never fires" rather than to
# "fires wrongly" — the safe direction for an operator-facing alert.
_memory: dict[str, float] = {}


def _key(camera_id: int, zone_id: int | None, name: str) -> str:
    return f"{_PREFIX}:{name}:{int(camera_id)}:{zone_id if zone_id is not None else '-'}"


def _redis():
    import redis
    from app.config import settings
    return redis.from_url(settings.redis_url, decode_responses=True,
                          socket_timeout=2)


def held_for(camera_id: int, zone_id: int | None, name: str, *,
             holding: bool, now: float | None = None,
             ttl_seconds: int = 900) -> float:
    """Seconds this condition has been continuously true. 0.0 when it is not.

    Call once per evaluation with ``holding`` set to whether the condition is
    true *right now*. The first true call starts the clock; the first false
    call clears it. The TTL cleans up cameras that stop reporting, so a
    camera going offline mid-crowd cannot leave a stale timer behind that
    fires the moment it returns.
    """
    now = time.time() if now is None else float(now)
    key = _key(camera_id, zone_id, name)

    if not holding:
        try:
            _redis().delete(key)
        except Exception as exc:                            # noqa: BLE001
            log.debug("sustain: redis clear failed (%s)", exc)
        _memory.pop(key, None)
        return 0.0

    try:
        r = _redis()
        # SET NX returns True only if we created it, i.e. the condition just
        # became true. One round trip, no read-modify-write race between the
        # several worker processes that may evaluate the same camera.
        if r.set(key, repr(now), nx=True, ex=ttl_seconds):
            return 0.0
        started = r.get(key)
        r.expire(key, ttl_seconds)
        return max(0.0, now - float(started)) if started else 0.0
    except Exception as exc:                                # noqa: BLE001
        log.warning("sustain: redis unavailable (%s) — in-memory fallback", exc)
        started = _memory.setdefault(key, now)
        return max(0.0, now - started)


def clear(camera_id: int, zone_id: int | None, name: str) -> None:
    """Forget the condition — used after firing so the clock restarts."""
    key = _key(camera_id, zone_id, name)
    try:
        _redis().delete(key)
    except Exception as exc:                                # noqa: BLE001
        log.debug("sustain: redis clear failed (%s)", exc)
    _memory.pop(key, None)


def fire_allowed(camera_id: int, zone_id: int | None, name: str, *,
                 cooldown_seconds: float, now: float | None = None) -> bool:
    """True at most once per ``cooldown_seconds`` for this key.

    Separate from :func:`held_for` because the cooldown has to outlive the
    slice too — an in-memory cooldown would let the same incident re-alert
    every 15 seconds, which is how an operator feed becomes unreadable.
    """
    now = time.time() if now is None else float(now)
    key = _key(camera_id, zone_id, name) + ":fired"
    ttl = max(1, int(cooldown_seconds))
    try:
        return bool(_redis().set(key, repr(now), nx=True, ex=ttl))
    except Exception as exc:                                # noqa: BLE001
        log.warning("sustain: redis cooldown unavailable (%s) — allowing", exc)
        last = _memory.get(key)
        if last is not None and now - last < cooldown_seconds:
            return False
        _memory[key] = now
        return True
