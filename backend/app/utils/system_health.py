"""System-health snapshot collector.

One implementation shared by BOTH the restricted GET /system-health
endpoint and the 08:00 EAT daily email task — same numbers in the
dashboard and the inbox, no drift.

Everything here is best-effort: a broken subsystem must show up AS
DATA (healthy: false / None / a name in collection_errors), never
crash the collector — a health page that 500s when something is
unhealthy is self-defeating. Every section runs behind _guard(),
which logs the full traceback and substitutes the section's default.

The API container has no docker socket, so "containers" are derived
from service heartbeats (DB ping, Redis ping, Celery worker ping,
streamer frame freshness) rather than `docker ps`; docker_images_gb
is None for the same reason. No psutil either — disk numbers come
from stdlib shutil.disk_usage + `du`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")

# Wall-clock the API process started — the api "container" uptime proxy.
_PROC_START = time.time()

# Detection types the alerts API labels URGENT — kept in sync lazily
# with app.api.alerts._SEVERITY_LABEL at call time, this is only the
# fallback if that import ever fails.
_URGENT_FALLBACK = {"intrusion", "weapon", "weapon_brandished", "fire",
                    "smoke", "fight", "fall", "trespass"}


def _fmt_uptime(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")


def _du_gb(path: str, timeout: int = 15) -> float | None:
    """Best-effort recursive size of a directory in GB via `du -sk`
    (kilobytes — portable). None when the path is missing or du times
    out on a huge tree."""
    if not path or not os.path.isdir(path):
        return None
    try:
        out = subprocess.run(["du", "-sk", path], capture_output=True,
                             text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return round(int(out.stdout.split()[0]) / (1024 ** 2), 2)
    except Exception:
        return None


def _urgent_types() -> set[str]:
    try:
        from app.api.alerts import _SEVERITY_LABEL
        return {dt for dt, lvl in _SEVERITY_LABEL.items() if lvl == "URGENT"}
    except Exception:
        return set(_URGENT_FALLBACK)


def _containers(r) -> list[dict]:
    """Service liveness derived from heartbeats (no docker socket in
    the api container). Each entry: {name, status, uptime, healthy}."""
    out: list[dict] = []

    # api — we're running, so it's up by construction.
    out.append({"name": "api", "status": "running",
                "uptime": _fmt_uptime(time.time() - _PROC_START),
                "healthy": True})

    # postgres — ping + server start time.
    try:
        from sqlalchemy import text
        from app.database import SessionLocal
        with SessionLocal() as db:
            started = db.execute(
                text("SELECT extract(epoch FROM now() - pg_postmaster_start_time())")
            ).scalar()
        out.append({"name": "postgres", "status": "running",
                    "uptime": _fmt_uptime(float(started) if started else None),
                    "healthy": True})
    except Exception as e:
        out.append({"name": "postgres", "status": f"error: {e.__class__.__name__}",
                    "uptime": None, "healthy": False})

    # redis — ping + INFO uptime.
    try:
        up = None
        if r is not None and r.ping():
            up = (r.info("server") or {}).get("uptime_in_seconds")
        out.append({"name": "redis", "status": "running" if up is not None else "unreachable",
                    "uptime": _fmt_uptime(up), "healthy": up is not None})
    except Exception as e:
        out.append({"name": "redis", "status": f"error: {e.__class__.__name__}",
                    "uptime": None, "healthy": False})

    # celery workers — 1s broadcast ping; each responder is a "container".
    try:
        from app.tasks.celery_app import celery_app
        pongs = celery_app.control.ping(timeout=1.0) or []
        names = sorted(k for p in pongs for k in p.keys())
        if names:
            for n in names:
                out.append({"name": n, "status": "running",
                            "uptime": None, "healthy": True})
        else:
            out.append({"name": "celery-workers", "status": "no workers responded",
                        "uptime": None, "healthy": False})
    except Exception as e:
        out.append({"name": "celery-workers", "status": f"error: {e.__class__.__name__}",
                    "uptime": None, "healthy": False})

    # streamer — healthy when ANY camera health key was written < 120s ago.
    try:
        fresh = False
        if r is not None:
            now = time.time()
            for key in r.scan_iter("vg:health:*", count=200):
                import json as _json
                raw = r.get(key)
                if not raw:
                    continue
                try:
                    h = _json.loads(raw)
                except Exception:
                    continue
                ts = h.get("last_health_at") or h.get("last_frame_at")
                if ts and (now - float(ts)) < 120:
                    fresh = True
                    break
        out.append({"name": "streamer",
                    "status": "running" if fresh else "no camera heartbeat < 120s",
                    "uptime": None, "healthy": fresh})
    except Exception as e:
        out.append({"name": "streamer", "status": f"error: {e.__class__.__name__}",
                    "uptime": None, "healthy": False})
    return out


# Section defaults — what a failed section reports instead of 500ing.
_DEFAULTS: dict[str, dict] = {
    "containers": {},   # list handled specially below
    "cameras": {"total": 0, "streaming": 0, "offline": 0,
                "offline_names": [], "ai_enabled_active": 0},
    "detection": {"events_last_30min_by_type": {}, "total_events_today": 0,
                  "events_by_hour_24h": []},
    "model": {"version": None, "map50": None, "precision": None,
              "recall": None, "deployed_since": None},
    "training": {"jobs_queued": 0, "jobs_running": 0,
                 "jobs_completed_today": 0, "latest_map50": None},
    "storage": {"total_gb": None, "used_gb": None, "free_gb": None,
                "percent_used": None, "docker_images_gb": None,
                "recordings_gb": None, "alert_clips_gb": None},
    "database": {"total_alerts": 0, "alerts_today": 0,
                 "detection_events_count": 0},
    "alerts": {"urgent_today": 0, "resolved_today": 0, "pending_today": 0},
    "integrations": {"bytetrack_active": False, "supervision_active": False,
                     "mannequin_filter_active": False},
}


def collect_system_health(db: Session) -> dict:
    """The full snapshot served by GET /system-health and rendered into
    the daily email. Pure read — no state changes anywhere. Sections
    that fail land in `collection_errors` with their default values and
    a full traceback in the api logs."""
    from app.models import AIModel, Alert, Camera, DetectionEvent, TrainingJob
    from app.stream.frame_buffer import FrameBuffer

    now_utc = datetime.now(timezone.utc)
    now = time.time()
    today_start = (now_utc.astimezone(EAT)
                   .replace(hour=0, minute=0, second=0, microsecond=0)
                   .astimezone(timezone.utc))

    errors: list[str] = []

    def guard(name: str, fn, default):
        try:
            return fn()
        except Exception:
            log.error("system-health: '%s' section failed:\n%s",
                      name, traceback.format_exc())
            # Clear any aborted transaction so ONE section's SQL error
            # doesn't poison every later section with "current
            # transaction is aborted, commands ignored...".
            try:
                db.rollback()
            except Exception:
                pass
            errors.append(name)
            return default

    r = None
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True,
                            socket_timeout=3)
    except Exception:
        pass

    # ---- cameras -----------------------------------------------------
    def _cameras() -> dict:
        cams = db.query(Camera).all()
        cam_ids = [c.id for c in cams]
        fb = FrameBuffer()
        health = fb.health_many(cam_ids) if cam_ids else {}
        hb_map: dict[int, float] = {}
        if r is not None and cam_ids:
            try:
                raws = r.mget([f"vg:inference-hb:{cid}" for cid in cam_ids])
                hb_map = {cid: float(v) for cid, v in zip(cam_ids, raws) if v}
            except Exception:
                pass
        streaming, offline = 0, []
        for c in cams:
            h = health.get(c.id) or {}
            lf = h.get("last_frame_at")
            if lf and (now - float(lf)) < 10 and (h.get("fps") or 0) > 0:
                streaming += 1
            else:
                offline.append(c.name or f"camera {c.id}")
        ai_active = sum(1 for c in cams
                        if c.ai_enabled and (now - hb_map.get(c.id, 0)) < 60)
        return {"total": len(cams), "streaming": streaming,
                "offline": len(cams) - streaming,
                "offline_names": offline[:25],
                "ai_enabled_active": ai_active}

    # ---- detection ----------------------------------------------------
    def _detection() -> dict:
        by_type = dict(
            db.query(DetectionEvent.detection_type, func.count(DetectionEvent.id))
              .filter(DetectionEvent.timestamp >= now_utc - timedelta(minutes=30))
              .group_by(DetectionEvent.detection_type).all())
        total_today = (db.query(func.count(DetectionEvent.id))
                         .filter(DetectionEvent.timestamp >= today_start)
                         .scalar() or 0)
        # Label the bucket and GROUP BY the label: two separate
        # func.date_trunc('hour', ...) calls bind 'hour' as two
        # DIFFERENT parameters ($1 vs $2), so Postgres rejected the
        # SELECT/GROUP BY pair with a GroupingError.
        hour_bucket = func.date_trunc(
            "hour", DetectionEvent.timestamp).label("hour_bucket")
        hourly_rows = (db.query(hour_bucket, func.count(DetectionEvent.id))
                         .filter(DetectionEvent.timestamp
                                 >= now_utc - timedelta(hours=24))
                         .group_by("hour_bucket")
                         .all())
        # h can be None (NULL-timestamp rows form a NULL group) — the
        # original .replace() on it was an AttributeError → 500.
        hour_counts: dict[datetime, int] = {}
        for h, n in hourly_rows:
            if h is None:
                continue
            if h.tzinfo is None:
                h = h.replace(tzinfo=timezone.utc)
            hour_counts[h] = int(n)
        events_by_hour = []
        anchor = now_utc.replace(minute=0, second=0, microsecond=0)
        for i in range(23, -1, -1):
            slot = anchor - timedelta(hours=i)
            events_by_hour.append({
                "hour": slot.astimezone(EAT).strftime("%H:00"),
                "count": int(hour_counts.get(slot, 0)),
            })
        return {"events_last_30min_by_type":
                    {k: int(v) for k, v in by_type.items() if k},
                "total_events_today": int(total_today),
                "events_by_hour_24h": events_by_hour}

    # ---- model ---------------------------------------------------------
    def _model() -> dict:
        dep = (db.query(AIModel).filter(AIModel.deployed.is_(True))
                 .order_by(AIModel.created_at.desc()).first())
        return {
            "version": f"{dep.name} {dep.version}" if dep else None,
            "map50": dep.map50 if dep else None,
            "precision": dep.precision if dep else None,
            "recall": dep.recall if dep else None,
            # No deployed_at column exists — created_at of the deployed
            # row is the closest honest proxy.
            "deployed_since": (dep.created_at.isoformat()
                               if dep and dep.created_at else None),
        }

    # ---- training -------------------------------------------------------
    def _training() -> dict:
        status_counts = dict(
            db.query(TrainingJob.status, func.count(TrainingJob.id))
              .filter(TrainingJob.status.in_(("queued", "running")))
              .group_by(TrainingJob.status).all())
        done_today = (db.query(func.count(TrainingJob.id))
                        .filter(TrainingJob.status == "done",
                                TrainingJob.completed_at >= today_start)
                        .scalar() or 0)
        latest_done = (db.query(TrainingJob)
                         .filter(TrainingJob.status == "done")
                         .order_by(TrainingJob.completed_at.desc()).first())
        return {"jobs_queued": int(status_counts.get("queued", 0)),
                "jobs_running": int(status_counts.get("running", 0)),
                "jobs_completed_today": int(done_today),
                "latest_map50": latest_done.best_map50 if latest_done else None}

    # ---- storage --------------------------------------------------------
    def _storage() -> dict:
        rec_dir = settings.recordings_dir
        try:
            du = shutil.disk_usage(rec_dir)
            total_gb = round(du.total / (1024 ** 3), 1)
            used_gb = round(du.used / (1024 ** 3), 1)
            free_gb = round(du.free / (1024 ** 3), 1)
            pct = round(du.used / du.total * 100, 1) if du.total else None
        except Exception:
            total_gb = used_gb = free_gb = pct = None
        return {"total_gb": total_gb, "used_gb": used_gb, "free_gb": free_gb,
                "percent_used": pct,
                # No docker socket in this container — honest null.
                "docker_images_gb": None,
                "recordings_gb": _du_gb(rec_dir),
                "alert_clips_gb": _du_gb(os.path.join(rec_dir, "clips"))}

    # ---- database ---------------------------------------------------------
    def _database() -> dict:
        return {
            "total_alerts": int(db.query(func.count(Alert.id)).scalar() or 0),
            "alerts_today": int(db.query(func.count(Alert.id))
                                  .filter(Alert.created_at >= today_start)
                                  .scalar() or 0),
            "detection_events_count":
                int(db.query(func.count(DetectionEvent.id)).scalar() or 0),
        }

    # ---- alerts ------------------------------------------------------------
    def _alerts() -> dict:
        urgent_set = _urgent_types()
        urgent_today = 0
        if urgent_set:
            urgent_today = (db.query(func.count(Alert.id))
                              .join(DetectionEvent,
                                    Alert.event_id == DetectionEvent.id)
                              .filter(Alert.created_at >= today_start,
                                      DetectionEvent.detection_type.in_(urgent_set))
                              .scalar() or 0)
        resolved_today = (db.query(func.count(Alert.id))
                            .filter(Alert.created_at >= today_start,
                                    Alert.status == "resolved").scalar() or 0)
        pending_today = (db.query(func.count(Alert.id))
                           .filter(Alert.created_at >= today_start,
                                   Alert.status.in_(("new", "acknowledged")))
                           .scalar() or 0)
        return {"urgent_today": int(urgent_today),
                "resolved_today": int(resolved_today),
                "pending_today": int(pending_today)}

    # ---- integrations ---------------------------------------------------------
    def _integrations() -> dict:
        def _importable(mod: str) -> bool:
            import importlib.util
            try:
                return importlib.util.find_spec(mod) is not None
            except Exception:
                return False
        return {
            # ByteTrack ships inside supervision.
            "bytetrack_active": _importable("supervision"),
            "supervision_active": _importable("supervision"),
            "mannequin_filter_active": bool(
                getattr(settings, "mannequin_filter_enabled", True)),
        }

    snap = {
        "generated_at": now_utc.isoformat(),
        "containers": guard("containers", lambda: _containers(r),
                            [{"name": "api", "status": "running",
                              "uptime": None, "healthy": True}]),
        "cameras": guard("cameras", _cameras, _DEFAULTS["cameras"]),
        "detection": guard("detection", _detection, _DEFAULTS["detection"]),
        "model": guard("model", _model, _DEFAULTS["model"]),
        "training": guard("training", _training, _DEFAULTS["training"]),
        "storage": guard("storage", _storage, _DEFAULTS["storage"]),
        "database": guard("database", _database, _DEFAULTS["database"]),
        "alerts": guard("alerts", _alerts, _DEFAULTS["alerts"]),
        "integrations": guard("integrations", _integrations,
                              _DEFAULTS["integrations"]),
        "collection_errors": errors,
    }
    return snap


def overall_status(snap: dict) -> tuple[str, str]:
    """(emoji, label) rollup used by the daily email + dashboard header.

    🔴 Critical: storage > 90%, zero cameras streaming, or a core
       service (postgres / redis) down.
    🟡 Warning: storage > 80%, any camera offline, no celery worker,
       a training backlog (> 5 queued), or any collector section
       failing.
    🟢 Healthy: everything else.
    """
    storage = snap.get("storage") or {}
    cams = snap.get("cameras") or {}
    core_down = any(not c.get("healthy")
                    for c in (snap.get("containers") or [])
                    if c.get("name") in ("postgres", "redis"))
    pct = storage.get("percent_used")
    if core_down or (pct is not None and pct > 90) or \
            (cams.get("total", 0) > 0 and cams.get("streaming", 0) == 0):
        return "🔴", "Critical"
    worker_down = any(not c.get("healthy")
                      for c in (snap.get("containers") or [])
                      if c.get("name") == "celery-workers")
    if (pct is not None and pct > 80) or cams.get("offline", 0) > 0 \
            or worker_down \
            or (snap.get("training") or {}).get("jobs_queued", 0) > 5 \
            or snap.get("collection_errors"):
        return "🟡", "Warning"
    return "🟢", "Healthy"
