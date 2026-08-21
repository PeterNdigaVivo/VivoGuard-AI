"""Evidence-backed positive operational alerts.

Positive alerts are deliberately separate from security detections: they are
silent, resolved on creation, ineligible for training, and excluded from
security-quality metrics.  Their purpose is to make verified recovery visible
without turning routine success into notification noise.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AgentReport, Alert, Camera, DetectionEvent


POSITIVE_TYPE = "positive_operational"


def emit_agent_recovery(
    db: Session, previous: AgentReport | None, current: AgentReport
) -> Alert | None:
    """Create one positive alert for a warning/critical -> ok transition."""
    if not settings.positive_agent_alerts_enabled:
        return None
    if previous is None or previous.status not in {"warning", "critical"}:
        return None
    if current.status != "ok" or previous.agent_name != current.agent_name:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.positive_agent_alert_dedup_hours
    )
    recent = (
        db.query(DetectionEvent.extra)
        .filter(
            DetectionEvent.detection_type == POSITIVE_TYPE,
            DetectionEvent.timestamp >= cutoff,
        )
        .order_by(DetectionEvent.id.desc())
        .limit(100)
        .all()
    )
    if any(
        (row[0] or {}).get("positive_kind") == "agent_recovery"
        and (row[0] or {}).get("source_agent") == current.agent_name
        for row in recent
    ):
        return None

    # DetectionEvent retains a non-null camera FK. Fleet positives use an
    # active camera only as a referential anchor; the API hides it because
    # scope=fleet. Prefer an online camera but do not fake a camera identity.
    camera = (
        db.query(Camera)
        .filter(Camera.deleted_at.is_(None))
        .order_by((Camera.status == "online").desc(), Camera.id.asc())
        .first()
    )
    if camera is None:
        return None

    now = datetime.now(timezone.utc)
    friendly = current.agent_name.replace("_", " ").title()
    extra = {
        "scope": "fleet",
        "positive_label": "POSITIVE – AUTOMATED",
        "positive_kind": "agent_recovery",
        "source_agent": current.agent_name,
        "verification_status": "automated",
        "title": f"Agent Recovered — {friendly}",
        "message": (
            f"{friendly} returned to normal after a {previous.status} run. "
            "The recovery is backed by consecutive agent reports."
        ),
        "evidence": {
            "previous_report_id": previous.id,
            "current_report_id": current.id,
            "previous_status": previous.status,
            "current_status": current.status,
        },
        "exclude_from_security_metrics": True,
        "notification_policy": "silent",
    }
    event = DetectionEvent(
        camera_id=camera.id,
        detection_type=POSITIVE_TYPE,
        confidence=1.0,
        bbox_json=[0.0, 0.0, 1.0, 1.0],
        extra=extra,
        timestamp=now,
        reviewed=True,
    )
    db.add(event)
    db.flush()
    alert = Alert(
        event_id=event.id,
        status="resolved",
        acknowledged_at=now,
        resolved_at=now,
        feedback_used_for_training=False,
        review_only=False,
        training_eligible=False,
        notification_suppressed=True,
    )
    db.add(alert)
    db.flush()
    return alert
