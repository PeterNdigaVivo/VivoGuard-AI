"""Feature-off incident/evidence/outbox shadow projection."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Alert, DetectionEvent
from app.models.incident import (
    DeliveryOutbox, EvidenceManifest, Incident, IncidentMember,
    IncidentTransition,
)


def event_idempotency_key(event_id: int) -> str:
    """Stable source key created where the event already has a durable ID."""
    return f"detection-event:{int(event_id)}:v1"


def _severity(event: DetectionEvent) -> str:
    priority = str((event.extra or {}).get("priority") or "").lower()
    if priority in {"critical", "high", "medium", "low", "info", "positive"}:
        return priority
    return "info"


def record_alert_foundations(
    db: Session,
    alert: Alert,
    event: DetectionEvent,
    *,
    store_id: int | None,
    queue_delivery: bool = False,
) -> Incident:
    """Idempotently create the additive projection for one persisted alert.

    The caller owns the transaction.  Production callers wrap this in a
    savepoint so projection failures cannot poison baseline alert persistence.
    """
    key = event_idempotency_key(event.id)
    existing_member = (
        db.query(IncidentMember)
        .filter(IncidentMember.idempotency_key == key)
        .one_or_none()
    )
    if existing_member is not None:
        return db.get(Incident, existing_member.incident_id)

    incident = Incident(
        incident_key=f"alert:{alert.id}",
        camera_id=event.camera_id,
        store_id=store_id,
        detection_type=event.detection_type,
        severity=_severity(event),
        current_state="provisional",
    )
    db.add(incident)
    db.flush()

    db.add(IncidentMember(
        incident_id=incident.id,
        alert_id=alert.id,
        event_id=event.id,
        source_event_uuid=str(uuid4()),
        idempotency_key=key,
    ))
    db.add(IncidentTransition(
        incident_id=incident.id,
        from_state=None,
        to_state="provisional",
        actor_type="system",
        reason_code="source_alert_persisted",
        evidence_json={"alert_id": alert.id, "event_id": event.id},
    ))

    clip_path = event.clip_path or (event.extra or {}).get("alert_clip_path")
    db.add(EvidenceManifest(
        alert_id=alert.id,
        snapshot_path=event.thumbnail_path,
        clip_path=clip_path,
        filmstrip_paths_json=alert.snapshot_paths,
        clip_eligible=None,
        clip_available=bool(clip_path),
        ineligible_reason=None,
    ))

    if queue_delivery and not alert.notification_suppressed:
        db.add(DeliveryOutbox(
            idempotency_key=f"{key}:baseline-realtime",
            incident_id=incident.id,
            alert_id=alert.id,
            channel="baseline_realtime",
            destination_ref="configured_baseline_destinations",
            payload_version="1.0",
            payload_json={
                "alert_id": alert.id,
                "event_id": event.id,
                "camera_id": event.camera_id,
                "detection_type": event.detection_type,
                "severity": incident.severity,
            },
        ))
    db.flush()
    return incident


def sync_evidence_manifest(db: Session, alert: Alert, event: DetectionEvent) -> bool:
    """Update a shadow manifest when asynchronous clip extraction finishes."""
    row = (
        db.query(EvidenceManifest)
        .filter(EvidenceManifest.alert_id == alert.id)
        .one_or_none()
    )
    if row is None:
        return False
    clip_path = event.clip_path or (event.extra or {}).get("alert_clip_path")
    row.snapshot_path = event.thumbnail_path
    row.clip_path = clip_path
    row.filmstrip_paths_json = alert.snapshot_paths
    row.clip_available = bool(clip_path)
    return True
