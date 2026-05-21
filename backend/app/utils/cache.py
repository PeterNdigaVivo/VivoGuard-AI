"""Redis-backed response cache for expensive analytics endpoints.

Pattern: version-keyed cache. Each store has an integer version
counter at `vg:cache:version:s{store_id}`. Cache keys embed the
current version, so when the inference worker writes new metrics
for that store, it INCRs the version and every older cache key is
instantly orphaned. Orphaned keys are cleaned up by their TTL.

Versioning + short TTL combo:
  • Eliminates the need to SCAN-and-DELETE on every write.
  • Bounds staleness to the TTL even when the version bumper hasn't
    fired (e.g. detector started writing but version-bump is async).
  • Keeps reads to one Redis GET + one MGET in the hot path.

Usage from a FastAPI route:

    @router.get("/store/{store_id}/live")
    def store_live(store_id: int, since=None, ..., db=Depends(get_db), _u=Depends(get_current_user)):
        return cache.get_or_compute(
            "store-live", store_id,
            params={"since": str(since), "until": str(until)},
            ttl=15,
            compute=lambda: _compute_live(store_id, since, until, db),
        )

Or via the @cached_store_endpoint decorator (preferred — leaves the
function body unchanged for existing endpoints).
"""
from __future__ import annotations
import functools
import hashlib
import json
import logging
from typing import Any, Callable

import redis

from app.config import settings

log = logging.getLogger(__name__)

_KEY_PREFIX = "vg:cache"
_VERSION_PREFIX = "vg:cache:version"


def _redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True)


def _store_version(r: redis.Redis, store_id: int | None) -> int:
    """Current cache version for a store. New stores start at 1."""
    if store_id is None:
        return 0
    v = r.get(f"{_VERSION_PREFIX}:s{store_id}")
    return int(v) if v else 1


def _params_hash(params: dict | None) -> str:
    """Stable, short hash of the params dict for the cache key."""
    if not params:
        return "_"
    # Filter None values so distinct query-string permutations that
    # mean the same thing land on the same key.
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return "_"
    payload = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:10]


def _make_key(name: str, store_id: int | None, version: int, params: dict | None) -> str:
    """vg:cache:{name}:s{store_id}:v{version}:{params_hash}"""
    h = _params_hash(params)
    return f"{_KEY_PREFIX}:{name}:s{store_id or 'na'}:v{version}:{h}"


def _json_default(o: Any) -> Any:
    """JSON serialiser that handles datetimes + Pydantic models +
    SQLAlchemy `Mapped` weirdness gracefully."""
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "__dict__"):
        # Last-ditch: drop SQLAlchemy internal `_sa_instance_state`.
        d = {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
        return d
    return str(o)


def get_or_compute(
    name: str,
    store_id: int | None,
    *,
    ttl: int,
    compute: Callable[[], Any],
    params: dict | None = None,
) -> Any:
    """Look up the cached value; on miss, call `compute()`, store
    the result with `ttl` seconds, and return it.

    Failures in Redis (network blip, OOM, etc.) are swallowed —
    cache must never break the API. We fall back to direct compute.
    """
    try:
        r = _redis()
        version = _store_version(r, store_id)
        key = _make_key(name, store_id, version, params)
        raw = r.get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception as e:
        log.warning("cache GET failed for %s (s=%s): %s", name, store_id, e)
        r = None       # type: ignore[assignment]

    value = compute()

    try:
        if r is None:
            r = _redis()
            version = _store_version(r, store_id)
            key = _make_key(name, store_id, version, params)
        r.set(key, json.dumps(value, default=_json_default), ex=ttl)
    except Exception as e:
        log.warning("cache SET failed for %s (s=%s): %s", name, store_id, e)
    return value


def bump_store_version(store_id: int | None) -> None:
    """Invalidate every cached value for this store. Cheap — one
    Redis INCR. Orphaned old-version keys age out via their TTL.

    Called from the inference worker after persisting a batch of
    metric_snapshots / detection_events.
    """
    if store_id is None:
        return
    try:
        _redis().incr(f"{_VERSION_PREFIX}:s{store_id}")
    except Exception as e:
        log.warning("cache version bump failed for s=%s: %s", store_id, e)


def cached_store_endpoint(name: str, ttl: int):
    """FastAPI route decorator. Caches the function's return value
    keyed on store_id + all other kwargs (excluding db / _u Depends
    objects).

    The wrapped function MUST take `store_id` as a positional or
    keyword argument — that's how we scope the cache.

        @cached_store_endpoint("store-live", ttl=15)
        @router.get("/store/{store_id}/live")
        def store_live_dashboard(store_id: int, since=None, ...):
            ...
    """
    EXCLUDED_KWARGS = {"db", "_u"}

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            store_id = kwargs.get("store_id")
            if store_id is None and args:
                # Positional first arg is usually store_id for these.
                store_id = args[0] if isinstance(args[0], int) else None
            # Build a params dict from kwargs minus the FastAPI
            # Depends() injections.
            params = {k: v for k, v in kwargs.items() if k not in EXCLUDED_KWARGS}
            return get_or_compute(
                name=name, store_id=store_id, ttl=ttl, params=params,
                compute=lambda: fn(*args, **kwargs),
            )
        return wrapper
    return deco
