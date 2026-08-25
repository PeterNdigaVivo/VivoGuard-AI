"""Sustained, deduplicated health watchdog for inference shards and GPU shadow."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import redis

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

STATE_KEY = "vg:inference:watchdog-state"
SENT_KEY = "vg:inference:watchdog-alert-sent"
AUTHORITATIVE_KEY = "vg:inference:health"
SHADOW_KEY = "vg:inference:batch-shadow-health"
SHADOW_EXPECTED_KEY = "vg:inference:batch-shadow-expected"


def _decode(raw: Any) -> dict | None:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def inference_health_problems(
    authoritative: dict | None,
    shadow: dict | None,
    *,
    shadow_expected: bool,
    now: float,
    max_shadow_age_seconds: float,
    max_schedule_wait_seconds: float,
) -> list[dict]:
    """Describe actionable failures without creating side effects."""
    problems: list[dict] = []
    for queue, health in ((authoritative or {}).get("inference_shards") or {}).items():
        if not isinstance(health, dict):
            continue
        depth = int(health.get("queue_depth") or 0)
        active = int(health.get("active") or 0)
        if depth > 0 and active == 0:
            problems.append({
                "code": "shard_not_consuming",
                "queue": str(queue),
                "queue_depth": depth,
                "assigned_cameras": int(health.get("cameras") or 0),
            })

    overdue = (authoritative or {}).get("critical_cameras_overdue")
    if overdue is not None and int(overdue) > 0:
        problems.append({
            "code": "critical_camera_gap_sla",
            "overdue_cameras": int(overdue),
            "camera_ids": list(
                (authoritative or {}).get("critical_camera_ids_overdue") or []
            ),
            "max_gap_seconds": (
                (authoritative or {}).get("critical_max_gap_seconds")
            ),
            "sla_seconds": int(
                (authoritative or {}).get("critical_gap_sla_seconds") or 0
            ),
        })

    standard_overdue = (authoritative or {}).get("standard_cameras_overdue")
    if standard_overdue is not None and int(standard_overdue) > 0:
        problems.append({
            "code": "standard_camera_gap_sla",
            "overdue_cameras": int(standard_overdue),
            "camera_ids": list(
                (authoritative or {}).get("standard_camera_ids_overdue") or []
            ),
            "max_gap_seconds": (
                (authoritative or {}).get("standard_max_gap_seconds")
            ),
            "sla_seconds": int(
                (authoritative or {}).get("standard_gap_sla_seconds") or 0
            ),
        })

    if not shadow_expected:
        return problems
    if shadow is None:
        problems.append({"code": "batch_shadow_missing"})
        return problems
    shadow_ts = float(shadow.get("last_run_ts") or 0.0)
    age = max(0.0, now - shadow_ts) if shadow_ts else None
    if age is None or age > max_shadow_age_seconds:
        problems.append({
            "code": "batch_shadow_stale",
            "age_seconds": round(age, 1) if age is not None else None,
        })
    if shadow.get("authoritative") is not False:
        problems.append({"code": "batch_shadow_unsafe_mode"})
    errors = int(shadow.get("errors") or 0)
    if errors > 0:
        problems.append({"code": "batch_shadow_errors", "errors": errors})
    wait = float(shadow.get("max_camera_schedule_wait_seconds") or 0.0)
    if wait > max_schedule_wait_seconds:
        problems.append({
            "code": "batch_shadow_schedule_wait",
            "wait_seconds": round(wait, 2),
        })
    return problems


def _signature(problems: list[dict]) -> str:
    # Backlog depth and telemetry age naturally change between polls. Outage
    # identity is the failure class plus shard; volatile evidence must not
    # reset the sustained-failure timer every minute.
    stable = [
        {"code": problem.get("code"), "queue": problem.get("queue")}
        for problem in problems
    ]
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _problem_summary(problems: list[dict]) -> str:
    parts = []
    for problem in problems:
        if problem["code"] == "shard_not_consuming":
            parts.append(
                f"{problem['queue']} has {problem['queue_depth']} queued task(s) "
                "and no active camera worker"
            )
        elif problem["code"] == "critical_camera_gap_sla":
            parts.append(
                f"{problem['overdue_cameras']} latency-critical camera(s) "
                f"exceeded the {problem.get('sla_seconds')}-second inference "
                f"gap SLA (maximum measured gap "
                f"{problem.get('max_gap_seconds')} seconds)"
            )
        elif problem["code"] == "standard_camera_gap_sla":
            parts.append(
                f"{problem['overdue_cameras']} standard camera(s) exceeded "
                f"the {problem.get('sla_seconds')}-second inference gap SLA "
                f"(maximum measured gap "
                f"{problem.get('max_gap_seconds')} seconds)"
            )
        elif problem["code"] == "batch_shadow_missing":
            parts.append("the expected GPU batch shadow has no health telemetry")
        elif problem["code"] == "batch_shadow_stale":
            parts.append(
                "the GPU batch shadow telemetry is stale "
                f"({problem.get('age_seconds')} seconds)"
            )
        elif problem["code"] == "batch_shadow_errors":
            parts.append(
                f"the GPU batch shadow recorded {problem.get('errors')} error(s)"
            )
        elif problem["code"] == "batch_shadow_schedule_wait":
            parts.append(
                "the GPU batch shadow camera wait exceeds its capacity limit "
                f"({problem.get('wait_seconds')} seconds)"
            )
        else:
            parts.append("the GPU batch process is not safely marked shadow-only")
    return "; ".join(parts)


@celery_app.task(name="inference.health_watchdog", ignore_result=True)
def inference_health_watchdog() -> None:
    """Alert once per sustained outage and automatically re-arm on recovery."""
    r = redis.from_url(settings.redis_url)
    now = time.time()
    try:
        authoritative = _decode(r.get(AUTHORITATIVE_KEY))
        shadow = _decode(r.get(SHADOW_KEY))
        shadow_expected = bool(r.get(SHADOW_EXPECTED_KEY))
        problems = inference_health_problems(
            authoritative,
            shadow,
            shadow_expected=shadow_expected,
            now=now,
            max_shadow_age_seconds=settings.inference_batch_health_max_age_seconds,
            max_schedule_wait_seconds=(
                settings.inference_batch_acceptance_max_wait_seconds
            ),
        )
    except Exception as exc:
        log.exception("inference watchdog telemetry read failed: %s", exc)
        return

    if not problems:
        try:
            r.delete(STATE_KEY, SENT_KEY)
        except Exception:
            pass
        return

    signature = _signature(problems)
    state = _decode(r.get(STATE_KEY)) or {}
    if state.get("signature") != signature:
        state = {"signature": signature, "first_seen_ts": now}
        r.set(STATE_KEY, json.dumps(state), ex=24 * 60 * 60)
        r.delete(SENT_KEY)
        return
    first_seen = float(state.get("first_seen_ts") or now)
    grace_seconds = settings.inference_watchdog_grace_seconds
    if any(problem["code"] == "critical_camera_gap_sla" for problem in problems):
        grace_seconds = min(
            grace_seconds,
            settings.inference_critical_watchdog_grace_seconds,
        )
    if now - first_seen < grace_seconds:
        return
    if r.get(SENT_KEY):
        return

    summary = _problem_summary(problems)
    body = (
        "VivoGuard inference capacity has a sustained fault: "
        f"{summary}. Existing camera/NVR health must be checked separately."
    )
    try:
        from app.database import SessionLocal
        from app.models import Camera
        from app.tasks.alerting import (
            _create_info_alert,
            _dashboard_recipients,
            _info_notification_allowed,
            _send_whatsapp,
        )

        created_event = None
        notification_allowed = False
        with SessionLocal() as db:
            row = (
                db.query(Camera.id)
                .filter(Camera.ai_enabled.is_(True), Camera.is_deleted.is_(False))
                .order_by(Camera.id.asc())
                .first()
            )
            if row:
                created_event = _create_info_alert(
                    db,
                    camera_id=int(row[0]),
                    zone_id=None,
                    store_id=None,
                    detection_type="system_health",
                    cls="inference_capacity_fault",
                    extra={
                        "priority": "high",
                        "scope": "fleet",
                        "component": "ai_inference_capacity",
                        "title": "Inference capacity fault",
                        "message": body,
                        "problems": problems,
                        "what_to_do": [
                            "Check the named inference worker queue and container",
                            "Check the GPU batch-shadow service when expected",
                            "Confirm recovery in System Health before closing",
                        ],
                    },
                )
                # Read persisted quality-control state while the ORM object is
                # still attached. SQLAlchemy expires attributes on commit;
                # consulting event.extra after this context closes raises a
                # DetachedInstanceError and previously prevented both the
                # notification and the watchdog dedup marker from being set.
                notification_allowed = _info_notification_allowed(created_event)
                db.commit()
        if not created_event:
            log.error("inference watchdog has no AI-enabled camera alert anchor")
            return
        if notification_allowed:
            recipients = _dashboard_recipients()
            if recipients:
                _send_whatsapp(recipients, f"🚨 {body}")
    except Exception as exc:
        log.exception("inference watchdog alert failed: %s", exc)
        return

    r.set(SENT_KEY, signature)
    log.warning("inference watchdog alert: %s", summary)
