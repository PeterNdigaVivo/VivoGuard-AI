"""The ONE definition of the camera fleet, its freshness and its health.

Alerts, System Health and Cameras used to compute their own totals from
different populations (ai_enabled only; non-deleted; deleted cameras
included) with different freshness windows (10 s, 30 s) and different
sources (Redis telemetry vs the lagging Camera.status column), so they
disagreed. Everything now reads ``fleet_status``.

Population: every camera that is not soft-deleted.

Per-camera coverage state (exactly one):
    unconfigured  AI disabled or not attached to a store (a set-up gap,
                  not a fault)
    offline       no frame for OFFLINE_AFTER_S, never seen, or the
                  streamer reports an error with no recent frame
    stale         last frame older than FRESH_S but newer than OFFLINE_AFTER_S
    fresh         a frame within FRESH_S

ai_active: fresh AND the worker processed frames recently (the
``vg:inference:last-run:{id}`` timestamp; ``vg:inference-hb`` holds a task
id, not a time, which is why the old AI-active count was always 0).

Health:
    critical  inference pipeline heartbeat missing/old, or no camera fresh,
              or fewer than half of the configured cameras fresh
    degraded  any configured camera stale/offline, cameras waiting for a
              worker, or a coverage SLA overdue
    healthy   otherwise

Coverage problems are reported here as coverage gaps; they are never
incidents in the operator queue.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

FRESH_S = 30.0
OFFLINE_AFTER_S = 10 * 60.0
PIPELINE_MAX_AGE_S = 10 * 60.0
AI_ACTIVE_MIN_WINDOW_S = 5 * 60.0

HEALTHY, DEGRADED, CRITICAL = "healthy", "degraded", "critical"


@dataclass
class CameraInput:
    camera_id: int
    name: str
    store_id: int | None
    store_name: str | None
    ai_enabled: bool
    last_frame_at: float | None = None
    fps: float = 0.0
    error: str | None = None
    inference_last_run: float | None = None


@dataclass
class FleetSummary:
    total: int = 0
    configured: int = 0
    fresh: int = 0
    stale: int = 0
    offline: int = 0
    unconfigured: int = 0
    ai_active: int = 0
    health: str = HEALTHY
    health_reasons: list[str] = field(default_factory=list)
    coverage_gaps: list[dict] = field(default_factory=list)


def classify_camera(c: CameraInput, *, now: float) -> str:
    if not c.ai_enabled or c.store_id is None:
        return "unconfigured"
    if c.last_frame_at is None:
        return "offline"
    age = max(0.0, now - c.last_frame_at)
    if age <= FRESH_S and c.fps > 0:
        return "fresh"
    if age <= OFFLINE_AFTER_S:
        return "stale"
    return "offline"


def summarize(cameras: list[CameraInput], *, now: float,
              pipeline: dict | None) -> FleetSummary:
    s = FleetSummary(total=len(cameras))
    rotation = 0.0
    try:
        rotation = float((pipeline or {}).get("estimated_full_rotation_seconds") or 0)
    except (TypeError, ValueError):
        rotation = 0.0
    ai_window = max(AI_ACTIVE_MIN_WINDOW_S, 2 * rotation)
    for c in cameras:
        state = classify_camera(c, now=now)
        if state == "unconfigured":
            s.unconfigured += 1
        else:
            s.configured += 1
            setattr(s, state, getattr(s, state) + 1)
            if (state == "fresh" and c.inference_last_run is not None
                    and now - c.inference_last_run <= ai_window):
                s.ai_active += 1
        if state != "fresh":
            age = (int(now - c.last_frame_at) if c.last_frame_at is not None
                   else None)
            s.coverage_gaps.append({
                "camera_id": c.camera_id, "camera_name": c.name,
                "store_id": c.store_id, "store_name": c.store_name,
                "state": state, "seconds_since_frame": age,
                "error": c.error if state != "unconfigured" else None,
                "reason": ("AI disabled" if not c.ai_enabled
                           else "not attached to a store" if c.store_id is None
                           else c.error or ("no frames yet" if age is None
                                            else f"no frame for {age}s")),
            })
    order = {"offline": 0, "stale": 1, "unconfigured": 2}
    s.coverage_gaps.sort(key=lambda g: (order[g["state"]], g["store_name"] or "",
                                        g["camera_name"] or ""))

    reasons_c, reasons_d = [], []
    pipe_age = None
    if pipeline and pipeline.get("last_run_ts"):
        try:
            pipe_age = now - float(pipeline["last_run_ts"])
        except (TypeError, ValueError):
            pipe_age = None
    if s.configured:
        if pipe_age is None or pipe_age > PIPELINE_MAX_AGE_S:
            reasons_c.append("inference pipeline heartbeat missing or older than 10 min")
        if s.fresh == 0:
            reasons_c.append("no configured camera has a fresh feed")
        elif s.fresh < s.configured / 2:
            reasons_c.append(f"only {s.fresh}/{s.configured} configured cameras fresh")
        if s.stale or s.offline:
            reasons_d.append(f"{s.stale} stale, {s.offline} offline cameras")
        p = pipeline or {}
        if int(p.get("cameras_waiting_for_worker") or 0) > 0:
            reasons_d.append(f"{int(p['cameras_waiting_for_worker'])} cameras waiting for a worker")
        overdue = (int(p.get("critical_cameras_overdue") or 0)
                   + int(p.get("standard_cameras_overdue") or 0))
        if overdue:
            reasons_d.append(f"{overdue} cameras past their coverage SLA")
    if reasons_c:
        s.health, s.health_reasons = CRITICAL, reasons_c + reasons_d
    elif reasons_d:
        s.health, s.health_reasons = DEGRADED, reasons_d
    return s


def _decode(raw) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def collect_inputs(db, r) -> tuple[list[CameraInput], dict | None]:
    """Read the canonical population and its telemetry (Redis optional)."""
    from app.models import Camera, Store

    rows = (db.query(Camera, Store.name)
              .outerjoin(Store, Store.id == Camera.store_id)
              .filter(Camera.is_deleted.is_(False))
              .order_by(Camera.id).all())
    ids = [c.id for c, _ in rows]
    health_raw, runs_raw, pipeline = [], [], None
    if r is not None and ids:
        try:
            health_raw = r.mget([f"vg:health:{i}" for i in ids])
            runs_raw = r.mget([f"vg:inference:last-run:{i}" for i in ids])
            pipeline = _decode(r.get("vg:inference:health"))
        except Exception:
            health_raw, runs_raw = [], []
    out = []
    for idx, (c, store_name) in enumerate(rows):
        h = _decode(health_raw[idx]) if idx < len(health_raw) else None
        h = h or {}
        run = runs_raw[idx] if idx < len(runs_raw) else None
        try:
            lf = float(h["last_frame_at"]) if h.get("last_frame_at") else None
        except (TypeError, ValueError):
            lf = None
        try:
            run_ts = float(run) if run else None
        except (TypeError, ValueError):
            run_ts = None
        try:
            fps = float(h.get("fps") or 0)
        except (TypeError, ValueError):
            fps = 0.0
        out.append(CameraInput(
            camera_id=c.id, name=c.name or f"camera {c.id}",
            store_id=c.store_id, store_name=store_name,
            ai_enabled=bool(c.ai_enabled), last_frame_at=lf, fps=fps,
            error=h.get("error"), inference_last_run=run_ts))
    return out, pipeline


def fleet_status(db, r, *, now: float | None = None) -> dict:
    """Canonical fleet totals, health and coverage gaps, plus the
    operational status card for the Alerts page."""
    from sqlalchemy import func

    from app.models import DetectionEvent
    from app.models.incident import Incident

    now_dt = datetime.now(timezone.utc)
    now = now if now is not None else now_dt.timestamp()
    cams, pipeline = collect_inputs(db, r)
    s = summarize(cams, now=now, pipeline=pipeline)

    def age(v):
        if v is None:
            return None
        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return max(0, int((now_dt - v).total_seconds()))

    latest_detection = db.query(func.max(DetectionEvent.timestamp)).scalar()
    latest_incident = (db.query(func.max(Incident.opened_at))
                         .filter(Incident.queue.in_(("incident", "review")))
                         .scalar())
    p = pipeline or {}
    pipe_age = None
    if p.get("last_run_ts"):
        try:
            pipe_age = max(0, int(now - float(p["last_run_ts"])))
        except (TypeError, ValueError):
            pipe_age = None
    return {
        "health": s.health,
        "health_reasons": s.health_reasons,
        "monitoring": ("offline" if pipe_age is None or pipe_age > PIPELINE_MAX_AGE_S
                       else "active" if s.health == HEALTHY else "degraded"),
        "totals": {"total": s.total, "configured": s.configured,
                   "fresh": s.fresh, "stale": s.stale, "offline": s.offline,
                   "unconfigured": s.unconfigured, "ai_active": s.ai_active},
        "queue": {
            "pipeline_age_seconds": pipe_age,
            "waiting_for_worker": int(p.get("cameras_waiting_for_worker") or 0),
            "queue_depth": p.get("inference_queue_depth"),
            "full_rotation_seconds": p.get("estimated_full_rotation_seconds"),
            "sla_overdue": (int(p.get("critical_cameras_overdue") or 0)
                            + int(p.get("standard_cameras_overdue") or 0)),
        },
        "latest_detection_age_seconds": age(latest_detection),
        "latest_incident_age_seconds": age(latest_incident),
        "coverage_gaps": s.coverage_gaps,
        "definitions": {"fresh_seconds": FRESH_S,
                        "offline_after_seconds": OFFLINE_AFTER_S},
    }
