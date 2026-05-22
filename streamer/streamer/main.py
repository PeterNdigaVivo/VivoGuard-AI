"""Standalone streamer service.

Loop:
  - read active cameras from Postgres (re-using the backend's models)
  - reconcile the StreamManager
  - sleep

Note: the streamer image is built FROM the backend image (or shares the
same base layer) so all `app.*` modules are available on PYTHONPATH.

Resilience rules (May-2026):

  1. Run alembic upgrade head on startup — same as the api container.
     This is the only way to guarantee that newly added columns (e.g.
     `transport` from migration 0006) actually exist before we SELECT
     them. Without it, a fresh-build api container that fails its
     migration leaves the streamer SELECTing a column the DB doesn't
     have, and EVERY camera goes dark.

  2. Use raw SQL (not the ORM) to read camera rows. The ORM SELECTs
     ALL mapped columns; if any one new column hasn't migrated yet,
     the whole query 500s and nothing streams. Raw SQL with an
     explicit column list, plus a fallback if any column is missing,
     keeps us streaming the cameras we CAN read about.
"""
from __future__ import annotations
import asyncio
import logging
import os
import socket
import sys
import time

# Backend package is mounted/copied at /app/app in the container.
sys.path.insert(0, "/app")

from sqlalchemy import text                                          # noqa: E402

try:
    from app.config   import settings                               # type: ignore
    from app.database import SessionLocal                           # type: ignore
    from app.utils.crypto  import decrypt                           # type: ignore
    from app.utils.network import build_rtsp_url                    # type: ignore
    from app.stream.manager import StreamManager, CameraSpec        # type: ignore
    from app.stream import auto_transport                           # type: ignore
except ImportError as e:                                            # pragma: no cover
    print(f"streamer: backend modules not on PYTHONPATH ({e})", file=sys.stderr)
    raise

log = logging.getLogger("streamer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

# Reconcile cadence. Lower = quicker pickup of newly added cameras.
# At 40+ cameras this is still cheap — just a single SELECT.
POLL_INTERVAL_SECONDS = int(os.environ.get("STREAMER_POLL_INTERVAL", "5"))

# Horizontal scaling. Set STREAMER_SHARD_COUNT > 1 across multiple
# streamer containers and give each a distinct STREAMER_SHARD_INDEX
# (0..N-1). Each replica filters cameras with `id % count == index`,
# so every camera lands on exactly one replica with no coordination
# needed. Cheap to scale — add more containers, increase count.
#
# Defaults (count=1, index=0) preserve single-process behaviour for
# the common case (Vivo's 21-camera fleet runs fine on one streamer).
SHARD_COUNT = max(1, int(os.environ.get("STREAMER_SHARD_COUNT", "1")))
SHARD_INDEX = max(0, int(os.environ.get("STREAMER_SHARD_INDEX", "0")))
if SHARD_INDEX >= SHARD_COUNT:
    raise SystemExit(
        f"streamer: STREAMER_SHARD_INDEX={SHARD_INDEX} must be < "
        f"STREAMER_SHARD_COUNT={SHARD_COUNT}"
    )

# --- Staggered startup tuning ----------------------------------------
# At 117 cameras a single reconcile pass spawned 117 FFmpeg subprocesses
# back-to-back; CPU + network + file-descriptor saturation made the
# whole batch take 2-3 minutes (and made the first ~30 cameras lose
# their RTSP handshake under load). We now:
#   1. TCP-probe every camera in parallel (asyncio.gather) with a
#      2 s timeout — total wall time ~3 s for 117 cameras.
#   2. Cache reachability for RETRY_UNREACHABLE_SECONDS — unreachable
#      cameras skip the probe until the cache entry expires.
#   3. Spawn FFmpeg workers in batches of STARTUP_BATCH_SIZE with
#      STARTUP_BATCH_DELAY_SECONDS sleep between batches.
TCP_CHECK_TIMEOUT_SECONDS = float(os.environ.get("STREAMER_TCP_CHECK_TIMEOUT", "2.0"))
RETRY_UNREACHABLE_SECONDS = int(os.environ.get("STREAMER_RETRY_UNREACHABLE", "300"))
STARTUP_BATCH_SIZE        = int(os.environ.get("STREAMER_STARTUP_BATCH_SIZE", "10"))
STARTUP_BATCH_DELAY_SECONDS = float(os.environ.get("STREAMER_STARTUP_BATCH_DELAY", "2.0"))

# camera_id -> (checked_at_epoch, reachable)
_reachable_cache: dict[int, tuple[float, bool]] = {}


def _run_migrations() -> None:
    """Wait for the DB schema to be at head — don't run migrations.

    HISTORY: this used to call alembic command.upgrade() so a streamer
    booting before (or alongside) the api container could self-heal a
    drifted schema. That worked at small scale, but at 117 cameras a
    pending migration (e.g. CREATE INDEX on metric_snapshots) holds
    the alembic_version row lock for many minutes. The streamer's
    upgrade() call then BLOCKS waiting for that lock — even when the
    DB is already at head — because alembic acquires the lock before
    it checks the current version. Observed: streamer frozen >20 min
    after "Will assume transactional DDL".

    The API container owns migrations; the streamer just waits up to
    `STREAMER_MIGRATION_WAIT_SECONDS` for the schema to look ready
    (an alembic_version row exists) and proceeds either way. The
    main loop's _query_active_cameras() already has a column-drop-
    and-retry fallback that handles a partially-migrated schema, so
    proceeding without waiting is safe even in the worst case.

    Set STREAMER_RUN_MIGRATIONS=1 in the env to opt back into the
    old behaviour — for ops who deploy the streamer container alone
    against a fresh DB.
    """
    if os.environ.get("STREAMER_RUN_MIGRATIONS", "0") == "1":
        _run_migrations_inline()
        return

    wait_seconds = int(os.environ.get("STREAMER_MIGRATION_WAIT_SECONDS", "30"))
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            from sqlalchemy import create_engine, text as _text
            eng = create_engine(settings.database_url, pool_pre_ping=True)
            with eng.connect() as conn:
                row = conn.execute(_text(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                )).fetchone()
            eng.dispose()
            if row:
                log.info("streamer: schema at alembic version %s — proceeding",
                         row[0])
                return
        except Exception as e:
            # alembic_version might not exist yet (very fresh DB) —
            # keep trying for a few seconds.
            log.debug("streamer: waiting on schema (%s)", e)
        time.sleep(2)
    log.warning("streamer: schema-check timed out after %ds — proceeding anyway. "
                "The main loop will retry queries on missing columns.",
                wait_seconds)


def _run_migrations_inline() -> None:
    """The ORIGINAL behaviour, kept behind STREAMER_RUN_MIGRATIONS=1.
    Don't use in production — see _run_migrations() docstring for the
    lock-contention foot-gun at scale."""
    try:
        from alembic.config import Config
        from alembic import command
        cfg_path = "/app/alembic.ini" if os.path.exists("/app/alembic.ini") else "alembic.ini"
        if not os.path.exists(cfg_path):
            log.warning("streamer: alembic.ini not found at %s — skipping auto-migrate", cfg_path)
            return
        cfg = Config(cfg_path)
        command.upgrade(cfg, "head")
        log.info("streamer: alembic upgrade head complete")
    except Exception as e:
        log.warning("streamer: alembic upgrade skipped: %s", e)


# Columns we read off `cameras`. Any column added in a NEW migration
# must be added here AND treated as optional in desired_specs() below,
# so a missing column never kills the reconcile.
_CAMERA_COLUMNS = [
    "id", "brand", "host", "rtsp_port", "http_port",
    "username", "password_encrypted", "channel_number",
    "rtsp_url_override", "inference_fps", "ai_enabled",
    "transport", "snapshot_url_override",
    "rtsp_transport",
]


def _snapshot_url_for(host: str, http_port: int, channel: int | None,
                      username: str, password: str,
                      override: str | None) -> str:
    """Compose the HTTP-snapshot URL for a camera.

    Override beats everything; otherwise build the Dahua default:
       http://USER:PASS@HOST:HTTP_PORT/cgi-bin/snapshot.cgi?channel=N
    """
    if override:
        return override
    from urllib.parse import quote
    user = quote(username or "", safe="")
    pw   = quote(password or "", safe="")
    auth = f"{user}:{pw}@" if user else ""
    ch   = channel or 1
    return f"http://{auth}{host}:{http_port}/cgi-bin/snapshot.cgi?channel={ch}"


def _query_active_cameras(db) -> list[dict]:
    """Read active cameras as plain dicts using explicit raw SQL.

    Tries the full column set first. If Postgres complains about a
    missing column (operator hasn't deployed the latest migration
    yet), strips the unknown column and retries — so legacy DBs keep
    streaming. Returns dicts so we don't depend on the ORM mapping
    matching the live DB shape.
    """
    cols = list(_CAMERA_COLUMNS)
    # Shard predicate. With defaults (count=1, index=0) this is
    # `MOD(id, 1) = 0`, i.e. matches every row — no behaviour change.
    # With count=3 across 3 streamer containers, each replica picks
    # up roughly a third of the cameras.
    #
    # Uses MOD() not the `%` operator. psycopg3 (current pg driver)
    # does NOT do the psycopg2-style `%%` → `%` escaping inside
    # bound queries when SQLAlchemy passes them as text(), so the
    # operator form leaked literal `%%` characters into the SQL and
    # Postgres rejected the query with
    #   operator does not exist: integer %% smallint
    shard_where = "AND MOD(id, :shard_count) = :shard_index"
    params = {"shard_count": SHARD_COUNT, "shard_index": SHARD_INDEX}
    while True:
        sql = (f"SELECT {', '.join(cols)} FROM cameras "
               f"WHERE ai_enabled = true {shard_where}")
        try:
            rows = db.execute(text(sql), params).mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            msg = str(e).lower()
            # Rollback the aborted transaction before retrying — otherwise
            # the next SELECT errors with "current transaction is aborted".
            try:
                db.rollback()
            except Exception:
                pass
            dropped = None
            for c in list(cols):
                if c == "id":
                    continue
                if f'column "{c}"' in msg or f'column cameras.{c}' in msg:
                    dropped = c
                    break
            if dropped is None:
                log.exception("streamer: camera query failed (unrecoverable): %s", e)
                raise
            cols.remove(dropped)
            log.warning("streamer: DB missing column 'cameras.%s' — falling back without it. "
                        "Run 'alembic upgrade head' to restore full functionality.", dropped)


def _persist_transport(db, camera_id: int, updates: dict) -> None:
    """Write auto-negotiated transport/port back to the cameras row.

    Uses raw SQL so a missing column never blocks the persistence
    (e.g. older DBs where `transport` doesn't exist will silently no-
    op — they don't need the switch anyway).
    """
    try:
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "id": camera_id}
        db.execute(text(f"UPDATE cameras SET {sets} WHERE id = :id"), params)
        db.commit()
        log.info("auto-transport: persisted %s for camera %s", updates, camera_id)
    except Exception as e:
        log.warning("auto-transport: could not persist %s for camera %s: %s",
                    updates, camera_id, e)
        try:
            db.rollback()
        except Exception:
            pass


def desired_specs() -> list[CameraSpec]:
    out: list[CameraSpec] = []
    with SessionLocal() as db:
        rows = _query_active_cameras(db)
        for r in rows:
            try:
                pw = decrypt(r.get("password_encrypted") or "")
            except Exception:
                pw = ""
            # Auto-negotiate transport BEFORE building the spec. If
            # RTSP/554 is unreachable but the Dahua snapshot CGI
            # responds on an HTTP port, switch this row to
            # http_snapshot and persist so the next reconcile picks
            # the right worker. No-op when RTSP works fine.
            try:
                updates = auto_transport.negotiate(r, pw)
                if updates:
                    _persist_transport(db, r["id"], updates)
                    r.update(updates)
            except Exception as e:
                log.warning("auto-transport: negotiation failed for camera %s: %s",
                            r.get("id"), e)
            transport = (r.get("transport") or "rtsp") or "rtsp"
            rtsp_url = build_rtsp_url(
                brand=r.get("brand"),
                host=r.get("host"),
                port=r.get("rtsp_port") or 554,
                username=r.get("username"),
                password=pw,
                channel=r.get("channel_number"),
                subtype=1,             # substream — cheaper for AI inference
                override=r.get("rtsp_url_override"),
            )
            snap_url = ""
            if transport == "http_snapshot":
                snap_url = _snapshot_url_for(
                    r.get("host"), r.get("http_port") or 80,
                    r.get("channel_number"),
                    r.get("username") or "", pw,
                    r.get("snapshot_url_override"),
                )
            out.append(CameraSpec(
                camera_id=r["id"],
                rtsp_url=rtsp_url,
                fps=r.get("inference_fps") or settings.inference_fps_default,
                width=640,
                transport=transport,
                snapshot_url=snap_url,
                username=r.get("username") or "",
                password=pw,
                rtsp_transport=(r.get("rtsp_transport") or "tcp") or "tcp",
            ))
    return out


def _spec_probe_endpoint(spec: CameraSpec) -> tuple[str, int] | None:
    """Pick (host, port) to TCP-probe for a CameraSpec.

    For RTSP-transport cameras we probe the RTSP socket the worker
    will open. For http_snapshot cameras we probe the HTTP port that
    the worker will poll. snapshot_url's host/port are parsed when
    available; otherwise we fall back to the rtsp_url's netloc.
    """
    from urllib.parse import urlsplit
    if spec.transport == "http_snapshot" and spec.snapshot_url:
        u = urlsplit(spec.snapshot_url)
        if u.hostname and u.port:
            return (u.hostname, u.port)
    if spec.rtsp_url:
        u = urlsplit(spec.rtsp_url)
        if u.hostname and u.port:
            return (u.hostname, u.port)
    return None


async def _tcp_open(host: str, port: int, timeout: float) -> bool:
    """Single async TCP probe — opens socket, closes immediately."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _async_check_all(specs: list[CameraSpec]) -> dict[int, bool]:
    """TCP-probe every spec in parallel. Total wall time ≈ the
    longest single timeout (2 s default), not N × 2 s."""
    async def _one(spec: CameraSpec) -> tuple[int, bool]:
        ep = _spec_probe_endpoint(spec)
        if not ep:
            return spec.camera_id, False
        ok = await _tcp_open(ep[0], ep[1], TCP_CHECK_TIMEOUT_SECONDS)
        return spec.camera_id, ok
    results = await asyncio.gather(*(_one(s) for s in specs), return_exceptions=False)
    return {cam_id: ok for cam_id, ok in results}


def _filter_reachable(specs: list[CameraSpec]) -> list[CameraSpec]:
    """Drop specs whose camera failed a recent TCP probe. Cameras
    that haven't been probed (or whose cache entry expired) get
    probed now — in parallel via asyncio.

    Returns the reachable subset. Unreachable cameras are written
    back into _reachable_cache with the current timestamp so we
    don't re-probe them until RETRY_UNREACHABLE_SECONDS has passed.
    """
    if not specs:
        return []
    now = time.time()
    need_check: list[CameraSpec] = []
    reachable: list[CameraSpec] = []
    for spec in specs:
        entry = _reachable_cache.get(spec.camera_id)
        if entry is not None:
            checked_at, was_reachable = entry
            if (now - checked_at) < RETRY_UNREACHABLE_SECONDS:
                if was_reachable:
                    reachable.append(spec)
                # Unreachable + within retry window → skip silently.
                continue
        need_check.append(spec)

    if not need_check:
        return reachable

    # Run the asyncio TCP probes. We're in a sync context, so use
    # asyncio.run() — creates a fresh loop each call. Cheap; ~3 s
    # at 117 cameras with 2 s timeout.
    t0 = time.monotonic()
    try:
        results = asyncio.run(_async_check_all(need_check))
    except Exception as e:
        log.warning("TCP health check failed (%s) — treating all as reachable", e)
        results = {s.camera_id: True for s in need_check}
    elapsed = time.monotonic() - t0
    log.info("health check: probed %d cameras in %.2fs", len(need_check), elapsed)

    for spec in need_check:
        ok = results.get(spec.camera_id, False)
        _reachable_cache[spec.camera_id] = (now, ok)
        if ok:
            reachable.append(spec)
    return reachable


def _reconcile_batched(mgr: StreamManager, specs: list[CameraSpec]) -> None:
    """Spawn FFmpeg workers in batches to avoid the CPU / network
    burst that comes from starting 117 subprocesses simultaneously.

    Calls mgr.reconcile() progressively with growing slices. Each
    intermediate call adds STARTUP_BATCH_SIZE new workers without
    stopping any (reconcile only stops workers whose camera_id is
    absent from `desired`; a strict superset never stops anyone)."""
    n = len(specs)
    if n == 0 or STARTUP_BATCH_SIZE <= 0 or n <= STARTUP_BATCH_SIZE:
        # Single shot — either nothing to do, batching disabled, or
        # the fleet fits in one batch.
        mgr.reconcile(specs)
        return
    total_batches = (n + STARTUP_BATCH_SIZE - 1) // STARTUP_BATCH_SIZE
    for i in range(0, n, STARTUP_BATCH_SIZE):
        end = min(i + STARTUP_BATCH_SIZE, n)
        batch_idx = (i // STARTUP_BATCH_SIZE) + 1
        log.info("Starting batch %d/%d (cameras %d-%d of %d)…",
                 batch_idx, total_batches, i + 1, end, n)
        mgr.reconcile(specs[:end])
        if end < n:
            time.sleep(STARTUP_BATCH_DELAY_SECONDS)


def main() -> None:
    log.info("VivoGuard streamer starting (shard %d of %d)", SHARD_INDEX, SHARD_COUNT)
    _run_migrations()
    log.info("streamer: migrations complete — entering reconcile loop")
    mgr = StreamManager()
    log.info("streamer: polling every %ss", POLL_INTERVAL_SECONDS)
    first_pass = True
    try:
        while True:
            try:
                t0 = time.monotonic()
                specs = desired_specs()
                spec_elapsed = time.monotonic() - t0
                total = len(specs)
                if first_pass:
                    log.info("VivoGuard streamer: %d cameras configured", total)

                # TCP pre-filter — drops unreachable cameras before
                # we burn FFmpeg subprocesses on them. Cached for
                # RETRY_UNREACHABLE_SECONDS so the next pass is
                # essentially free.
                reachable = _filter_reachable(specs)
                offline = total - len(reachable)
                if first_pass:
                    log.info("Health check: %d reachable, %d offline (skipped)",
                             len(reachable), offline)
                    log.info("Starting RTSP streams for %d reachable cameras…",
                             len(reachable))
                    _reconcile_batched(mgr, reachable)
                    log.info("All reachable cameras streaming")
                    first_pass = False
                else:
                    # Subsequent passes: reconcile is idempotent and
                    # cheap — just stream whatever's reachable now.
                    log.info("streamer: %d/%d cameras reachable (probe cost %.2fs)",
                             len(reachable), total, spec_elapsed)
                    mgr.reconcile(reachable)
            except Exception as e:
                # Don't let one bad poll kill the loop. Sleep and retry —
                # the most common cause (migration still running) clears
                # itself within a few seconds.
                log.exception("reconcile failed: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop_all()


if __name__ == "__main__":
    main()
