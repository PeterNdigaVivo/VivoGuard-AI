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
import logging
import os
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

# Hard cap on simultaneously-streaming cameras for this replica.
# At 117 cameras the streamer container hit CPU + FD limits before
# inference fell behind. The cap forces an ordered priority queue
# instead of an unbounded fanout: cameras with zones configured first
# (active detection), then open-store cameras, then everything else.
# Set to 0 to disable the cap.
MAX_CONCURRENT_STREAMS = max(0, int(os.environ.get("MAX_CONCURRENT_STREAMS", "50")))


def _run_migrations() -> None:
    """Bring the DB schema to head before we start polling.

    Without this, a streamer that boots before (or alongside) the api
    container can SELECT a column the database doesn't have yet, and
    every reconcile fails until the api finishes migrating. Cheap to
    run — a no-op when already at head — and self-healing when not.
    """
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
    # Used by the priority sort (open-store cameras stream first).
    "store_id",
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
    # `id % 1 = 0`, i.e. matches every row — no behaviour change.
    # With count=3 across 3 streamer containers, each replica picks
    # up roughly a third of the cameras.
    shard_where = "AND (id %% :shard_count) = :shard_index"
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


# Background pool for auto-transport probes. At 117 cameras with a
# cold cache, doing these synchronously inside desired_specs took
# 60+ minutes — the streamer appeared hung. Now: each row gets
# scheduled in a small pool, the hot path returns immediately, and
# the next reconcile (5s later) picks up any DB updates.
import threading
from concurrent.futures import ThreadPoolExecutor

# Cameras whose probe is already queued / running. We don't enqueue
# again until the worker clears the entry, so cold start doesn't
# fan out a 117-task storm.
_probe_in_flight: set[int] = set()
_probe_in_flight_lock = threading.Lock()
_probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")


def _schedule_negotiation(row: dict, password_plain: str) -> None:
    """Queue a one-shot auto-transport probe for this camera in the
    background pool. Returns IMMEDIATELY — the hot reconcile loop
    never blocks on network probes. When the probe finishes and
    updates the cameras row, the NEXT reconcile reads the new
    transport/port and spawns the right worker class.
    """
    cam_id = row.get("id")
    if cam_id is None:
        return
    with _probe_in_flight_lock:
        if cam_id in _probe_in_flight:
            return       # already queued / running
        _probe_in_flight.add(cam_id)

    def _do_probe():
        try:
            updates = auto_transport.negotiate(row, password_plain)
            if updates:
                # Each thread opens its own session — never share a
                # SQLAlchemy Session across threads.
                with SessionLocal() as bg_db:
                    _persist_transport(bg_db, cam_id, updates)
        except Exception as e:
            log.warning("background probe failed for camera %s: %s", cam_id, e)
        finally:
            with _probe_in_flight_lock:
                _probe_in_flight.discard(cam_id)

    try:
        _probe_pool.submit(_do_probe)
    except Exception:
        # Pool shut down — clear the flight flag so a future call retries.
        with _probe_in_flight_lock:
            _probe_in_flight.discard(cam_id)


def _camera_priority(row: dict, zone_cam_ids: set[int],
                      open_store_ids: set[int]) -> tuple[int, int]:
    """Lower tuple sorts FIRST. Three priority tiers:
       0 — has zones AND store is open right now (active detection)
       1 — has zones (off-hours)
       2 — open store but no zones
       3 — everything else
    Tie-breaker on camera_id for deterministic ordering."""
    has_zone = row["id"] in zone_cam_ids
    is_open  = (row.get("store_id") in open_store_ids) if row.get("store_id") else False
    if has_zone and is_open: tier = 0
    elif has_zone:           tier = 1
    elif is_open:            tier = 2
    else:                    tier = 3
    return (tier, row["id"])


def desired_specs() -> list[CameraSpec]:
    out: list[CameraSpec] = []
    with SessionLocal() as db:
        rows = _query_active_cameras(db)

        # Priority sort is best-effort: the cap and the ordering are
        # nice-to-have, not required for the streamer to function. If
        # ANY part of this block fails, we keep `rows` exactly as
        # _query_active_cameras returned and stream every ai_enabled
        # camera. The previous shape had two narrow try/excepts that
        # could leave the SQLAlchemy session in an aborted state
        # between them; this single outer try with a rollback before
        # the inner queries fixes that.
        try:
            try:
                db.rollback()           # clean slate before priority queries
            except Exception:
                pass
            zone_cam_ids: set[int] = set()
            try:
                zone_rows = db.execute(text(
                    "SELECT DISTINCT camera_id FROM zones WHERE suppressed = false"
                )).mappings().all()
                zone_cam_ids = {r["camera_id"] for r in zone_rows}
            except Exception as e:
                log.warning("priority: zones query failed: %s", e)
                try:
                    db.rollback()
                except Exception:
                    pass

            open_store_ids: set[int] = set()
            try:
                from app.models import Store
                from app.utils.business_hours import is_store_open
                for s in db.query(Store).filter(Store.is_active == True).all():  # noqa: E712
                    try:
                        if is_store_open(s):
                            open_store_ids.add(s.id)
                    except Exception:
                        continue        # one bad row never aborts the sort
            except Exception as e:
                log.warning("priority: could not enumerate open stores: %s", e)
                try:
                    db.rollback()
                except Exception:
                    pass

            rows.sort(key=lambda r: _camera_priority(r, zone_cam_ids, open_store_ids))
            if MAX_CONCURRENT_STREAMS and len(rows) > MAX_CONCURRENT_STREAMS:
                dropped = len(rows) - MAX_CONCURRENT_STREAMS
                log.info("priority cap: streaming top %d of %d cameras (%d deferred)",
                         MAX_CONCURRENT_STREAMS, len(rows), dropped)
                rows = rows[:MAX_CONCURRENT_STREAMS]
        except Exception as e:
            log.warning("priority sort failed (%s) — streaming all eligible cameras", e)
            try:
                db.rollback()
            except Exception:
                pass
        for r in rows:
            try:
                pw = decrypt(r.get("password_encrypted") or "")
            except Exception:
                pw = ""
            # Auto-transport probes are now scheduled to a background
            # thread (see _schedule_negotiation below). The hot path
            # returns the camera's CURRENT DB state immediately so the
            # streamer can spawn workers without waiting on 30-60s of
            # network probes per camera. When a probe finishes and
            # updates the row, the NEXT reconcile picks up the new
            # transport — usually within ~10s.
            _schedule_negotiation(r, pw)
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


def main() -> None:
    log.info("streamer: starting up (shard %d of %d, max_concurrent=%d)",
             SHARD_INDEX, SHARD_COUNT, MAX_CONCURRENT_STREAMS)
    _run_migrations()
    log.info("streamer: migrations complete — entering reconcile loop")
    mgr = StreamManager()
    log.info("streamer: polling every %ss", POLL_INTERVAL_SECONDS)
    try:
        while True:
            try:
                t0 = time.monotonic()
                specs = desired_specs()
                elapsed = time.monotonic() - t0
                # Log every loop so silence is unambiguous diagnostic
                # data. At 117 cameras the elapsed should be <1s now
                # that probes are background-scheduled.
                log.info("streamer: %d desired cameras (computed in %.2fs)",
                         len(specs), elapsed)
                mgr.reconcile(specs)
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
        try:
            _probe_pool.shutdown(wait=False)
        except Exception:
            pass


if __name__ == "__main__":
    main()
