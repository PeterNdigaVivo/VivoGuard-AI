"""Autonomous AI monitoring agents.

Ten domain agents plus a watchdog, each a Celery beat task on the `alerts`
queue (so they never compete with camera inference). Each domain agent works
in two stages:

  1. SENSE  — gather deterministic telemetry for its domain (bounded DB /
     Redis reads only; the `_run_*` / `_sense_*` functions below).
  2. REASON — hand that telemetry to Claude (`_ai_reason`), which diagnoses
     status, root cause, gaps, and natural-language recommendations. This is
     what makes them AI agents rather than threshold scripts.

If the LLM is disabled or unreachable, the agent falls back to its rule-based
verdict, so the "never break" guarantees still hold. The watchdog stays fully
deterministic ON PURPOSE — the component that detects dead agents must not
itself depend on an external API.

Every run writes one structured row to `agent_reports`.

Resilience (see RULE 1-5 in the build spec):
  * heartbeat — each agent stamps vg:agent:hb:{name} at the START of a run
    with a TTL of 3x its schedule, so one missed run never false-alarms.
  * circuit breaker — vg:agent:fails:{name} counts consecutive failures;
    at 5 the agent suspends itself for 1h (vg:agent:suspended:{name}) and
    fires an ops alert, so a broken agent can't spam every cycle.
  * watchdog — agents.agent_watchdog (every 10 min) re-enqueues any agent
    whose heartbeat has aged past 2x its interval, so agents never silently
    stop. Suspended agents are skipped.

Scheduling: clock-aligned/daily agents use crontab() (celery timezone is
Africa/Nairobi = EAT); sub-hour agents use plain intervals. Registration is
in celery_app.py (beat_schedule + task_routes).

Resource discipline: every DB read is COUNT/GROUP-BY or LIMIT-bounded; the
Simulation agent processes at most 20 cameras per run via a Redis cursor and
calls gc.collect() between cameras.

Agent-level URGENT notifications (watchdog DEAD, circuit-breaker SUSPEND) go
to the ops WhatsApp/dashboard channel rather than the camera-bound Alert
table (DetectionEvent.camera_id is NOT NULL, so a system alert has no camera
to attach to).
"""
from __future__ import annotations

import gc
import json
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

import redis
from celery.exceptions import SoftTimeLimitExceeded, MaxRetriesExceededError

from app.config import settings
from app.tasks.celery_app import celery_app
from app.agent_control.policies import AGENT_POLICIES

log = logging.getLogger(__name__)

# ── schedule map (seconds) — drives heartbeat TTL + watchdog liveness ────
AGENT_INTERVAL_SECONDS: dict[str, int] = {
    name: int(policy["interval_seconds"]) for name, policy in AGENT_POLICIES.items()
}
CIRCUIT_MAX_FAILS = 5
SUSPEND_SECONDS   = 3600
FAILS_TTL_SECONDS = 24 * 3600
SIM_MAX_CAMERAS   = 30

# ── AI reasoning layer ───────────────────────────────────────────────────
# The role each agent adopts when it reasons with Claude. Every domain agent
# is AI-backed; the watchdog is not (see module docstring).
AGENT_ROLES: dict[str, str] = {
    "ml_dataset":       "ML Dataset Curator",
    "training":         "ML Training Operations analyst",
    "backend_health":   "Backend Reliability engineer",
    "frontend":         "Frontend / edge-delivery analyst",
    "db_admin":         "Database Administrator",
    "streamer":         "Camera-streaming reliability analyst",
    "simulation":       "Detector QA / simulation analyst",
    "detector_alerts":  "Detection & alerts analyst",
    "retail_standards": "Retail standards & store-operations analyst",
    "inspection":       "Chief inspection analyst compiling the daily brief",
}
# Hybrid (Option B):
#   * RULE-BASED (no LLM) — high-frequency / liveness agents where reasoning
#     adds latency+cost but no value: backend_health, streamer,
#     detector_alerts (+ the watchdog, which is always deterministic).
#   * HAIKU — the analytical agents: ml_dataset, training, frontend, db_admin,
#     simulation.
#   * SONNET (the default model) — the two daily strategic agents:
#     retail_standards and inspection.
# Inspection reasons INSIDE _run_inspection (so its Claude narrative can be
# sent over WhatsApp), so it is intentionally NOT in AI_AGENTS — that keeps
# the generic layer from making a second LLM call.
AI_AGENTS = {"ml_dataset", "training", "frontend", "db_admin", "simulation",
             "retail_standards"}
AGENT_MODEL_OVERRIDES: dict[str, str] = {
    "ml_dataset": "claude-haiku-4-5",
    "training":   "claude-haiku-4-5",
    "frontend":   "claude-haiku-4-5",
    "db_admin":   "claude-haiku-4-5",
    "simulation": "claude-haiku-4-5",
    # retail_standards + inspection use the Sonnet default (agents_llm_model).
}


# ── shared infra ─────────────────────────────────────────────────────────
def _redis():
    """String-decoded client for the agent bookkeeping keys (separate from
    the bytes frame-buffer client)."""
    return redis.from_url(settings.redis_url, decode_responses=True)


def _hb_key(name: str) -> str:        return f"vg:agent:hb:{name}"
def _fails_key(name: str) -> str:     return f"vg:agent:fails:{name}"
def _suspended_key(name: str) -> str: return f"vg:agent:suspended:{name}"


def _heartbeat(r, name: str) -> None:
    ttl = AGENT_INTERVAL_SECONDS.get(name, 3600) * 3
    try:
        r.set(_hb_key(name), int(time.time()), ex=ttl)
    except Exception as e:                       # never let a hb write break a run
        log.warning("agent %s heartbeat write failed: %s", name, e)


def _is_suspended(r, name: str) -> bool:
    try:
        return r.get(_suspended_key(name)) is not None
    except Exception:
        return False


def _note_success(r, name: str) -> None:
    try:
        r.delete(_fails_key(name))
    except Exception:
        pass


def _note_failure(r, name: str) -> None:
    """Increment the consecutive-failure counter; trip the breaker at 5."""
    try:
        n = r.incr(_fails_key(name))
        r.expire(_fails_key(name), FAILS_TTL_SECONDS)
    except Exception:
        return
    if n >= CIRCUIT_MAX_FAILS:
        try:
            r.set(_suspended_key(name), int(time.time()), ex=SUSPEND_SECONDS)
            # Reset the counter on trip so that after the 1h suspension the
            # agent needs 5 FRESH failures to re-trip — not just one.
            r.delete(_fails_key(name))
        except Exception:
            pass
        _ops_alert("AGENT SUSPENDED",
                   f"Agent '{name}' suspended for 1h after {n} consecutive "
                   f"failures. It will resume automatically.")


def _ops_alert(kind: str, body: str) -> None:
    """Best-effort ops notification. Uses the existing WhatsApp channel
    (currently a logging no-op) + always logs. Never raises."""
    msg = f"[VivoGuard agents] {kind}: {body}"
    try:
        from app.tasks.alerting import _dashboard_recipients
        from app.tasks.briefings import _send_whatsapp
        _send_whatsapp(_dashboard_recipients(), msg)
    except Exception as e:
        log.warning("ops alert delivery failed: %s", e)
    log.warning(msg)


def _write_report(name: str, status: str, findings: dict | None = None, *,
                  actions_taken: dict | list | None = None,
                  gaps: dict | list | None = None,
                  duration_ms: int = 0,
                  error_message: str | None = None) -> None:
    """Persist one agent_reports row. Swallows its own errors — a failed
    report write must never mask the agent's actual result."""
    try:
        from app.database import SessionLocal
        from app.models import AgentReport
        with SessionLocal() as db:
            db.add(AgentReport(
                agent_name=name, status=status,
                findings=findings, actions_taken=actions_taken, gaps=gaps,
                duration_ms=duration_ms, error_message=error_message,
            ))
            db.commit()
    except Exception as e:
        log.exception("agent %s report write failed: %s", name, e)


def _eat_now() -> datetime:
    # Always Africa/Nairobi (EAT) — NOT settings.app_timezone, which is UTC
    # on this deployment. The daily agents' "today" boundaries + digests must
    # be EAT regardless of the app's clock.
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Nairobi"))
    except Exception:
        return datetime.now(timezone.utc)


def _eat_day_start_utc() -> datetime:
    """00:00 today in EAT, expressed as tz-aware UTC — for 'today' filters."""
    local = _eat_now()
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def _safe(findings: dict, key: str, fn):
    """Run one metric probe; on failure record it under _errors instead of
    failing the whole agent (schema drift shouldn't trip the breaker)."""
    try:
        findings[key] = fn()
    except Exception as e:
        findings.setdefault("_errors", {})[key] = str(e)


# ── AI reasoning (the "agent brain") ─────────────────────────────────────
def _extract_json(text: str) -> dict | None:
    """Pull the first JSON OBJECT out of an LLM response (tolerates code
    fences / stray prose). Returns None for a non-dict reply (a top-level
    array or scalar) so callers cleanly fall back to the rule-based result —
    a malformed LLM response must never crash the agent."""
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _ai_reason(name: str, role: str, telemetry: dict,
               *, model: str | None = None) -> dict | None:
    """Ask Claude to diagnose this agent's telemetry. Returns a dict
    {status, summary, findings, gaps, recommended_actions} or None if the
    LLM is disabled / no key / SDK missing / any error (caller falls back
    to the rule-based verdict). Never raises."""
    if not getattr(settings, "agents_llm_enabled", True):
        return None
    api_key = getattr(settings, "anthropic_api_key", "") or ""
    if not api_key:
        return None
    try:
        import anthropic
    except Exception:
        log.warning("agents: anthropic SDK not installed — LLM reasoning off")
        return None
    mdl = model or getattr(settings, "agents_llm_model", "claude-sonnet-5")
    timeout = float(getattr(settings, "agents_llm_timeout_seconds", 45))
    system = (
        f"You are the {role} for VivoGuard-AI, an AI video-surveillance and "
        "retail-intelligence platform running ~102 cameras across ~26 Vivo "
        "fashion stores in Kenya/Uganda/Rwanda on CPU-only inference. You are "
        "an autonomous monitoring agent. You are given a JSON snapshot of your "
        "domain's telemetry. Reason about it like an expert on call: judge "
        "health, infer likely root cause, and give concrete next actions. "
        "Respond with ONLY a JSON object (no markdown, no prose outside it) "
        "with keys: status ('ok'|'warning'|'critical'), summary (one plain-"
        "English sentence), findings (object of the notable facts), gaps "
        "(array of short strings — problems/risks), recommended_actions "
        "(array of short imperative strings). Ground every statement in the "
        "telemetry; never invent numbers."
    )
    user = ("Telemetry JSON for this run:\n"
            + json.dumps(telemetry, default=str)[:12000])
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        msg = client.messages.create(
            model=mdl, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = " ".join(getattr(b, "text", "") or "" for b in (msg.content or [])).strip()
        return _extract_json(text)
    except Exception as e:
        log.warning("agent %s LLM reasoning failed (model=%s): %s", name, mdl, e)
        return None


def _apply_ai(name: str, result: dict) -> dict:
    """Overlay Claude's diagnosis on the deterministic result. The rule-based
    `result` is the telemetry + fallback verdict; if the LLM answers we let
    it set status/gaps/recommendations, otherwise we keep the fallback."""
    telemetry = result.get("findings", {}) or {}
    verdict = _ai_reason(name, AGENT_ROLES.get(name, "monitoring agent"),
                         telemetry, model=AGENT_MODEL_OVERRIDES.get(name))
    if not verdict:
        findings = dict(telemetry)
        findings["ai"] = {"used": False, "reason": "llm unavailable/disabled"}
        result["findings"] = findings
        return result
    status = verdict.get("status")
    if status not in ("ok", "warning", "critical"):
        status = result.get("status", "ok")
    gaps = list(result.get("gaps") or [])
    for g in (verdict.get("gaps") or []):
        if g not in gaps:
            gaps.append(g)
    actions: dict = {}
    prior = result.get("actions_taken")
    if isinstance(prior, dict):
        actions.update(prior)
    elif prior:
        actions["deterministic"] = prior
    if verdict.get("recommended_actions"):
        actions["ai_recommendations"] = verdict["recommended_actions"]
    return {
        "status": status,
        "findings": {"telemetry": telemetry,
                     "ai": {"used": True,
                            "model": AGENT_MODEL_OVERRIDES.get(
                                name, getattr(settings, "agents_llm_model", "")),
                            "summary": verdict.get("summary"),
                            "findings": verdict.get("findings")}},
        "gaps": gaps or None,
        "actions_taken": actions or None,
    }


# ── the wrapper (RULE 1) ─────────────────────────────────────────────────
def _execute(task, name: str, fn) -> None:
    """Shared agent lifecycle: suspend-check → heartbeat → run → report,
    with soft-time-limit handling, circuit breaker, and bounded retries.
    `fn()` returns a dict {status, findings, actions_taken?, gaps?}."""
    started = time.time()
    r = _redis()
    if _is_suspended(r, name):
        log.info("agent %s is suspended — skipping this cycle", name)
        return
    _heartbeat(r, name)
    try:
        result = fn() or {}
        # AI reasoning layer — hand the deterministic telemetry to Claude
        # for diagnosis. Falls back to the rule-based result when the LLM
        # is disabled/unreachable, so the agent never breaks.
        if name in AI_AGENTS:
            result = _apply_ai(name, result)
        _write_report(
            name, result.get("status", "ok"),
            result.get("findings", {}),
            actions_taken=result.get("actions_taken"),
            gaps=result.get("gaps"),
            duration_ms=int((time.time() - started) * 1000),
        )
        _note_success(r, name)
    except SoftTimeLimitExceeded:
        _write_report(name, "warning", {"error": "soft time limit exceeded"},
                      duration_ms=int((time.time() - started) * 1000))
        _note_failure(r, name)
    except Exception as exc:                                   # noqa: BLE001
        # Retry first; only WRITE the critical report + count the breaker
        # failure ONCE, after all retries are exhausted. Doing it before the
        # retry made each of the 4 attempts write a duplicate report and
        # increment the counter 4x (tripping the 5-fail breaker in ~2 cycles).
        try:
            raise task.retry(exc=exc)
        except MaxRetriesExceededError:
            _write_report(name, "critical",
                          {"error": str(exc), "traceback": traceback.format_exc()},
                          duration_ms=int((time.time() - started) * 1000),
                          error_message=str(exc)[:500])
            _note_failure(r, name)     # one failure per beat cycle


# ══════════════════════════════════════════════════════════════════════════
# AGENT DOMAIN LOGIC  (_run_* return {status, findings, actions_taken?, gaps?})
# ══════════════════════════════════════════════════════════════════════════
def _run_ml_dataset() -> dict:
    from sqlalchemy import func
    from app.database import SessionLocal
    from app.models import Dataset, TrainingImage
    f: dict = {}
    gaps: list = []
    with SessionLocal() as db:
        _safe(f, "datasets", lambda: db.query(func.count(Dataset.id)).scalar() or 0)
        _safe(f, "total_images", lambda: db.query(func.count(TrainingImage.id)).scalar() or 0)
        _safe(f, "unlabeled_images", lambda: db.query(func.count(TrainingImage.id))
              .filter(TrainingImage.labeled.is_(False)).scalar() or 0)
        try:
            empty = (db.query(Dataset.id, Dataset.name)
                       .outerjoin(TrainingImage, TrainingImage.dataset_id == Dataset.id)
                       .group_by(Dataset.id, Dataset.name)
                       .having(func.count(TrainingImage.id) == 0)
                       .limit(50).all())
            if empty:
                gaps.append({"empty_datasets": [{"id": i, "name": n} for i, n in empty]})
        except Exception as e:
            f.setdefault("_errors", {})["empty_datasets"] = str(e)
    if f.get("unlabeled_images", 0) > 500:
        gaps.append({"labeling_backlog": f"{f['unlabeled_images']} images unlabeled"})
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_training() -> dict:
    from sqlalchemy import func
    from app.database import SessionLocal
    from app.models import TrainingJob, AIModel
    f: dict = {}
    gaps: list = []
    actions: list = []
    with SessionLocal() as db:
        _safe(f, "queued_jobs", lambda: db.query(func.count(TrainingJob.id))
              .filter(TrainingJob.status == "queued").scalar() or 0)
        _safe(f, "running_jobs", lambda: db.query(func.count(TrainingJob.id))
              .filter(TrainingJob.status == "running").scalar() or 0)
        _safe(f, "deployed_models", lambda: db.query(func.count(AIModel.id))
              .filter(AIModel.deployed.is_(True)).scalar() or 0)
        # Stale running jobs (>12h) — the dispatcher normally sweeps these;
        # flag if any linger so the watchdog trail shows it.
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
            stale = (db.query(func.count(TrainingJob.id))
                       .filter(TrainingJob.status == "running",
                               TrainingJob.started_at < cutoff).scalar() or 0)
            f["stale_running_jobs"] = stale
            if stale:
                gaps.append({"stale_jobs": f"{stale} jobs running >12h"})
        except Exception as e:
            f.setdefault("_errors", {})["stale_running_jobs"] = str(e)
    if f.get("queued_jobs", 0) > 20:
        gaps.append({"queue_backlog": f"{f['queued_jobs']} jobs queued"})
    return {"status": "warning" if gaps else "ok",
            "findings": f, "gaps": gaps or None, "actions_taken": actions or None}


def _run_backend_health() -> dict:
    from sqlalchemy import text
    from app.database import SessionLocal
    f: dict = {}
    gaps: list = []
    # DB connectivity.
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        f["db"] = "ok"
    except Exception as e:
        f["db"] = "down"; gaps.append({"db": str(e)[:200]})
    # Redis connectivity.
    r = _redis()
    try:
        r.ping(); f["redis"] = "ok"
    except Exception as e:
        f["redis"] = "down"; gaps.append({"redis": str(e)[:200]})
    # Inference supervisor breadcrumb freshness (written every 30s).
    try:
        raw = r.get("vg:inference:health")
        age = None
        if raw is not None:
            try:
                age = int(time.time()) - int(json.loads(raw).get("ts"))
            except Exception:
                age = int(time.time()) - int(float(raw))
        f["inference_health_age_s"] = age
        if age is None or age > 600:
            gaps.append({"inference": f"health breadcrumb stale (age={age}s)"})
    except Exception as e:
        f.setdefault("_errors", {})["inference_health"] = str(e)
    return {"status": "critical" if f.get("db") == "down" or f.get("redis") == "down"
            else ("warning" if gaps else "ok"),
            "findings": f, "gaps": gaps or None}


def _run_frontend() -> dict:
    """Tail the last 100 nginx access-log lines for 5xx/asset errors. The
    worker may not share the nginx container's filesystem — that's a gap,
    not a failure."""
    f: dict = {}
    gaps: list = []
    path = getattr(settings, "nginx_access_log", "/var/log/nginx/access.log")
    f["log_path"] = path
    try:
        from collections import deque
        with open(path, "r", errors="ignore") as fh:
            lines = deque(fh, maxlen=100)
        f["lines_scanned"] = len(lines)
        err5xx = sum(1 for ln in lines if any(f' {c} ' in ln for c in
                     ("500", "502", "503", "504")))
        notfound = sum(1 for ln in lines if " 404 " in ln)
        f["http_5xx"] = err5xx
        f["http_404"] = notfound
        if err5xx:
            gaps.append({"server_errors": f"{err5xx} 5xx in last 100 requests"})
    except FileNotFoundError:
        gaps.append({"log_access": f"nginx log not reachable from worker ({path})"})
    except Exception as e:
        f.setdefault("_errors", {})["log_tail"] = str(e)
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_db_admin() -> dict:
    """Table sizes/rows via pg_stat_user_tables + pg_relation_size — no
    table scans. Postgres only; degrades gracefully on SQLite."""
    from sqlalchemy import text
    from app.database import SessionLocal
    f: dict = {}
    gaps: list = []
    with SessionLocal() as db:
        dialect = db.bind.dialect.name if db.bind else "unknown"
        f["dialect"] = dialect
        if dialect != "postgresql":
            f["note"] = "size stats only available on postgres"
            return {"status": "ok", "findings": f}
        rows = db.execute(text(
            "SELECT relname, n_live_tup, "
            "pg_relation_size(quote_ident(relname)) AS bytes "
            "FROM pg_stat_user_tables "
            "ORDER BY bytes DESC LIMIT 20")).all()
        f["tables"] = [{"table": r[0], "rows": int(r[1] or 0),
                        "bytes": int(r[2] or 0)} for r in rows]
        _safe(f, "db_size_bytes", lambda: int(db.execute(
            text("SELECT pg_database_size(current_database())")).scalar() or 0))
        big = [t for t in f.get("tables", []) if t["bytes"] > 5 * 1024**3]
        if big:
            gaps.append({"large_tables": [t["table"] for t in big]})
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_streamer() -> dict:
    """Which ai_enabled cameras have a fresh vg:frame:{id} (TTL 30s)."""
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Camera
    f: dict = {}
    gaps: list = []
    with SessionLocal() as db:
        cam_ids = [c for (c,) in db.execute(
            select(Camera.id).where(Camera.ai_enabled.is_(True))).all()]
    f["ai_enabled_cameras"] = len(cam_ids)
    r = _redis()
    dark: list = []
    streaming = 0
    for cid in cam_ids:
        try:
            if r.exists(f"vg:frame:{cid}"):
                streaming += 1
            else:
                dark.append(cid)
        except Exception:
            dark.append(cid)
    f["streaming"] = streaming
    f["dark"] = len(dark)
    if dark:
        gaps.append({"dark_cameras": dark[:50]})   # bounded list
    return {"status": "warning" if dark else "ok", "findings": f, "gaps": gaps or None}


def _run_simulation() -> dict:
    """Run the in-memory YOLO model over up to 20 rotating cameras' latest
    cached frame and flag ones producing zero detections (possible silent
    detector/stream). Rotates via vg:sim:cursor; gc.collect() per camera."""
    import numpy as np
    import cv2
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Camera
    from app.stream.frame_buffer import FrameBuffer
    from app.ai.yolov8_runner import infer

    with SessionLocal() as db:
        cam_ids = sorted(c for (c,) in db.execute(
            select(Camera.id).where(Camera.ai_enabled.is_(True))).all())
    f: dict = {"total_cameras": len(cam_ids)}
    gaps: list = []
    if not cam_ids:
        return {"status": "ok", "findings": f}

    r = _redis()
    try:
        cursor = int(r.get("vg:sim:cursor") or 0)
    except Exception:
        cursor = 0
    cursor %= len(cam_ids)

    batch = [cam_ids[(cursor + i) % len(cam_ids)]
             for i in range(min(SIM_MAX_CAMERAS, len(cam_ids)))]
    fb = FrameBuffer()
    processed = 0
    no_frame: list = []
    zero_det: list = []
    for cid in batch:
        try:
            jpeg = fb.latest_jpeg(int(cid))
            if not jpeg:
                no_frame.append(cid)
                continue
            arr = np.frombuffer(jpeg, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                no_frame.append(cid)
                continue
            dets = infer(frame, conf=0.25)
            processed += 1
            if not dets:
                zero_det.append(cid)
        except Exception as e:
            f.setdefault("_errors", {})[str(cid)] = str(e)
        finally:
            gc.collect()                      # RULE: reclaim per-camera memory

    # Advance the cursor so the next run covers the following slice.
    try:
        r.set("vg:sim:cursor", (cursor + len(batch)) % len(cam_ids))
    except Exception:
        pass
    f.update(cameras_this_run=len(batch), processed=processed,
             no_frame=len(no_frame), zero_detection=len(zero_det),
             cursor_next=(cursor + len(batch)) % len(cam_ids))
    if no_frame:
        gaps.append({"no_frame_cameras": no_frame[:20]})
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_detector_alerts() -> dict:
    from sqlalchemy import func
    from app.database import SessionLocal
    from app.models import DetectionEvent, Alert
    f: dict = {}
    gaps: list = []
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    with SessionLocal() as db:
        by_type = (db.query(DetectionEvent.detection_type, func.count(DetectionEvent.id))
                     .filter(DetectionEvent.timestamp >= since)
                     .group_by(DetectionEvent.detection_type).all())
        f["events_15min"] = {t: int(n) for t, n in by_type}
        f["events_total_15min"] = sum(int(n) for _, n in by_type)
        _safe(f, "alerts_15min", lambda: db.query(func.count(Alert.id))
              .filter(Alert.created_at >= since).scalar() or 0)
    if f.get("events_total_15min", 0) == 0:
        gaps.append({"no_detections": "0 detection events in last 15 min"})
    if f.get("alerts_15min", 0) > 200:
        gaps.append({"alert_flood": f"{f['alerts_15min']} alerts in 15 min"})
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_retail_standards() -> dict:
    """Per-store aggregates for today (EAT), GROUP BY store — no per-store
    loops."""
    from sqlalchemy import func
    from app.database import SessionLocal
    from app.models import Camera, DetectionEvent, Store
    f: dict = {}
    gaps: list = []
    since = _eat_day_start_utc()
    with SessionLocal() as db:
        _safe(f, "active_stores", lambda: db.query(func.count(Store.id))
              .filter(Store.is_active.is_(True)).scalar() or 0)
        uni = (db.query(Camera.store_id, func.count(DetectionEvent.id))
                 .join(Camera, Camera.id == DetectionEvent.camera_id)
                 .filter(DetectionEvent.timestamp >= since,
                         DetectionEvent.detection_type == "uniform_compliance")
                 .group_by(Camera.store_id).all())
        f["uniform_violations_by_store"] = {str(s): int(n) for s, n in uni if s}
        active = (db.query(func.count(func.distinct(Camera.store_id)))
                    .join(DetectionEvent, DetectionEvent.camera_id == Camera.id)
                    .filter(DetectionEvent.timestamp >= since).scalar() or 0)
        f["stores_with_activity_today"] = int(active)
        silent = (f.get("active_stores", 0) or 0) - int(active)
        if silent > 0:
            gaps.append({"silent_stores": f"{silent} active stores with no events today"})
    return {"status": "warning" if gaps else "ok", "findings": f, "gaps": gaps or None}


def _run_inspection() -> dict:
    """Compile the last 24h of agent_reports into a digest, prune rows older
    than 30 days, and send the digest to the ops channel."""
    from sqlalchemy import func
    from app.database import SessionLocal
    from app.models import AgentReport
    f: dict = {}
    actions: list = []
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with SessionLocal() as db:
        rows = (db.query(AgentReport.agent_name, AgentReport.status,
                         func.count(AgentReport.id))
                  .filter(AgentReport.run_at >= since)
                  .group_by(AgentReport.agent_name, AgentReport.status).all())
        summary: dict = {}
        for name, status, n in rows:
            summary.setdefault(name, {})[status] = int(n)
        f["summary_24h"] = summary
        f["agents_reporting"] = len(summary)
        missing = [a for a in AGENT_INTERVAL_SECONDS if a not in summary]
        f["agents_missing_24h"] = missing
        # Prune >30 days.
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        deleted = (db.query(AgentReport)
                     .filter(AgentReport.run_at < cutoff)
                     .delete(synchronize_session=False))
        db.commit()
        actions.append({"pruned_reports": int(deleted)})
    crit = sum(v.get("critical", 0) for v in f["summary_24h"].values())
    warn = sum(v.get("warning", 0) for v in f["summary_24h"].values())

    # AI narrative (Sonnet) BEFORE sending, so the WhatsApp digest IS the
    # Claude-written brief. Falls back to a deterministic one-liner if the
    # LLM is unavailable — the digest always goes out.
    status: str | None = None
    verdict = _ai_reason("inspection", AGENT_ROLES["inspection"], f)
    if verdict and verdict.get("summary"):
        body = "🤖 " + str(verdict["summary"])
        recs = verdict.get("recommended_actions") or []
        if recs:
            body += "\nActions: " + "; ".join(str(x) for x in recs[:5])
        f["ai"] = {"used": True, "model": getattr(settings, "agents_llm_model", ""),
                   "summary": verdict.get("summary"),
                   "findings": verdict.get("findings"),
                   "recommended_actions": recs or None}
        if verdict.get("status") in ("ok", "warning", "critical"):
            status = verdict["status"]
    else:
        body = (f"Daily agent inspection — {f['agents_reporting']}/10 agents "
                f"reported in 24h, {crit} critical, {warn} warnings"
                + (f", missing: {', '.join(f['agents_missing_24h'])}"
                   if f["agents_missing_24h"] else ""))
        f["ai"] = {"used": False, "reason": "llm unavailable/disabled"}

    _ops_alert("DAILY INSPECTION", body)
    actions.append({"digest_sent": True})
    if status is None:
        status = "critical" if (crit or f["agents_missing_24h"]) else \
                 ("warning" if warn else "ok")
    return {"status": status, "findings": f, "actions_taken": actions}


# ══════════════════════════════════════════════════════════════════════════
# CELERY TASKS  (RULE 1 wrapper via _execute)
# ══════════════════════════════════════════════════════════════════════════
_AGENT_KW = dict(ignore_result=True, bind=True, max_retries=3,
                 default_retry_delay=60, soft_time_limit=55, time_limit=60)


@celery_app.task(name="agents.ml_dataset", **_AGENT_KW)
def agent_ml_dataset(self):        _execute(self, "ml_dataset", _run_ml_dataset)


@celery_app.task(name="agents.training", **_AGENT_KW)
def agent_training(self):          _execute(self, "training", _run_training)


@celery_app.task(name="agents.backend_health", **_AGENT_KW)
def agent_backend_health(self):    _execute(self, "backend_health", _run_backend_health)


@celery_app.task(name="agents.frontend", **_AGENT_KW)
def agent_frontend(self):          _execute(self, "frontend", _run_frontend)


@celery_app.task(name="agents.db_admin", **_AGENT_KW)
def agent_db_admin(self):          _execute(self, "db_admin", _run_db_admin)


@celery_app.task(name="agents.streamer", **_AGENT_KW)
def agent_streamer(self):          _execute(self, "streamer", _run_streamer)


@celery_app.task(name="agents.simulation", **_AGENT_KW)
def agent_simulation(self):        _execute(self, "simulation", _run_simulation)


@celery_app.task(name="agents.detector_alerts", **_AGENT_KW)
def agent_detector_alerts(self):   _execute(self, "detector_alerts", _run_detector_alerts)


@celery_app.task(name="agents.retail_standards", **_AGENT_KW)
def agent_retail_standards(self):  _execute(self, "retail_standards", _run_retail_standards)


@celery_app.task(name="agents.inspection", **_AGENT_KW)
def agent_inspection(self):        _execute(self, "inspection", _run_inspection)


# Name → task, for the watchdog re-enqueue + the manual-trigger API.
AGENT_TASKS = {
    "ml_dataset":       agent_ml_dataset,
    "training":         agent_training,
    "backend_health":   agent_backend_health,
    "frontend":         agent_frontend,
    "db_admin":         agent_db_admin,
    "streamer":         agent_streamer,
    "simulation":       agent_simulation,
    "detector_alerts":  agent_detector_alerts,
    "retail_standards": agent_retail_standards,
    "inspection":       agent_inspection,
}


# ══════════════════════════════════════════════════════════════════════════
# WATCHDOG (RULE 2) — every 10 min
# ══════════════════════════════════════════════════════════════════════════
@celery_app.task(name="agents.agent_watchdog", ignore_result=True, bind=True,
                 soft_time_limit=25, time_limit=30)
def agent_watchdog(self):
    started = time.time()
    r = _redis()
    now = int(time.time())
    dead: list = []
    revived: list = []
    suspended: list = []
    for name, interval in AGENT_INTERVAL_SECONDS.items():
        if _is_suspended(r, name):
            suspended.append(name)
            continue
        try:
            raw = r.get(_hb_key(name))
        except Exception:
            raw = None
        age = None if raw is None else now - int(raw)
        # Dead = no heartbeat at all, or heartbeat older than 2x interval.
        if age is None or age >= 2 * interval:
            dead.append({"agent": name, "hb_age_s": age})
            task = AGENT_TASKS.get(name)
            try:
                if task is not None:
                    task.delay()
                else:
                    celery_app.send_task(AGENT_POLICIES[name]["task"])
                revived.append(name)
            except Exception as e:
                log.warning("watchdog could not re-enqueue %s: %s", name, e)
    if dead:
        _ops_alert("AGENTS DEAD",
                   "re-enqueued: " + ", ".join(revived) if revived
                   else "no heartbeat: " + ", ".join(d["agent"] for d in dead))
    _write_report("watchdog", "critical" if dead else "ok",
                  findings={"checked": len(AGENT_INTERVAL_SECONDS),
                            "dead": dead, "suspended": suspended},
                  actions_taken={"revived": revived} if revived else None,
                  duration_ms=int((time.time() - started) * 1000))
