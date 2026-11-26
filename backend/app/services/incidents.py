"""Authoritative incident lifecycle.

One continuing event = one Incident = one operator action.

``attach_detection`` is called once per promoted detection, inside the
caller's transaction, after the Alert row (kept for audit) is flushed:

* an OPEN incident with the same dedup key absorbs the new evidence
  (timeline row, event_count, last_seen_at) and the new alert is marked
  ``notification_suppressed`` so nothing pages twice;
* if the gap since that incident's last evidence is longer than the dedup
  window the operator is reminded once (``renotified``), but it is still
  the same incident;
* evidence arriving shortly after an operator closed the incident is kept
  on the closed incident (``after_close``) instead of re-opening the same
  noise, except for urgent events, which open a fresh incident;
* otherwise a new incident is opened.

At most one open incident per dedup key is guaranteed by the partial
unique index ``uq_incidents_open_dedup_key``; the insert runs in a
savepoint and a lost race re-reads the winner and appends to it.

Everything that counts or lists incidents goes through ``incident_query``
so tab counts, badges, the summary, exports and bulk actions agree.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from app.config import settings
from app.models import Alert, DetectionEvent
from app.models.incident import (
    INCIDENT_CLOSED_STATUSES, INCIDENT_OPEN_STATUSES, Incident, IncidentMember,
    IncidentTransition,
)
from app.services import alert_policy as policy

log = logging.getLogger(__name__)

# Store-level conditions are deduplicated per store, whichever camera
# happened to anchor the evidence.
STORE_SCOPED_TYPES = frozenset({"shop_open_close"})
# shop_open_close rules that prove the store has opened.
OPENING_RULES_PREFIX = "shop_opened"

VIEWS = ("open", "attention", "review", "resolved", "all")
ACTIONS = ("acknowledge", "resolve", "dismiss", "escalate")
_PRIORITY_RANK = {"urgent": 3, "attention": 2, "low": 1}

_schema_ok: bool | None = None


# --------------------------------------------------------------------- gate

def lifecycle_ready(db: Session) -> bool:
    """Enabled by config AND the 0050 schema is present.

    The schema check runs once per process. A worker that starts before
    the API has migrated keeps the legacy path instead of failing every
    insert; it picks the lifecycle up on its next restart.
    """
    global _schema_ok
    if not bool(getattr(settings, "incident_lifecycle_enabled", False)):
        return False
    if _schema_ok is None:
        try:
            from sqlalchemy import inspect
            insp = inspect(db.get_bind())
            inc = {c["name"] for c in insp.get_columns("incidents")}
            alr = {c["name"] for c in insp.get_columns("alerts")}
            _schema_ok = {"dedup_key", "status", "queue"} <= inc and "incident_id" in alr
            if not _schema_ok:
                log.error("incident lifecycle enabled but migration 0050 is "
                          "not applied; staying on the legacy alert path")
        except Exception as exc:  # pragma: no cover - defensive
            log.error("incident lifecycle schema check failed: %s", exc)
            return False
    return bool(_schema_ok)


def _reset_schema_cache() -> None:  # tests
    global _schema_ok
    _schema_ok = None


# --------------------------------------------------------------------- keys

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dedup_key(*, store_id: int | None, camera_id: int | None,
              zone_id: int | None, detection_type: str,
              rule: str | None) -> str:
    cam = "*" if detection_type in STORE_SCOPED_TYPES else (camera_id or "-")
    zone = zone_id if zone_id is not None else "-"
    return f"s{store_id or '-'}:c{cam}:z{zone}:{detection_type}:{rule or '-'}"


def window_for(detection_type: str) -> timedelta:
    overrides = getattr(settings, "incident_dedup_window_overrides", None) or {}
    seconds = overrides.get(detection_type,
                            getattr(settings, "incident_dedup_window_seconds", 900))
    return timedelta(seconds=max(30, int(seconds)))


def _rule_of(event: DetectionEvent) -> str | None:
    ex = event.extra or {}
    rule = ex.get("rule") or ex.get("cls")
    return str(rule)[:64] if rule else None


# ------------------------------------------------------------ policy input

def normalise_tags(raw) -> frozenset[str]:
    """Zone tags, tolerating the double-encoded JSON some rows hold."""
    import json
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
        if isinstance(raw, (str, bytes)):
            raw = [raw]
    return frozenset(str(t) for t in (raw or []))


def zone_tags_for(db: Session, zone_id: int | None) -> frozenset[str]:
    if zone_id is None:
        return frozenset()
    from app.models import Zone
    raw = db.query(Zone.detection_types_json).filter(Zone.id == zone_id).scalar()
    return normalise_tags(raw)


def store_has_schedule(db: Session, store_id: int | None) -> bool:
    if store_id is None:
        return False
    from app.models import Store
    hours = db.query(Store.business_hours_json).filter(Store.id == store_id).scalar()
    return bool(hours)


def build_policy_input(db: Session, *, detection_type: str, confidence: float,
                       extra: dict | None, zone_id: int | None,
                       store_id: int | None, frames_seen: int | None = None,
                       suppressed_reason: str | None = None,
                       raw_review: bool = False,
                       zone_tags: frozenset[str] | None = None,
                       ) -> policy.PolicyInput:
    """Gather the context the policy needs, using settings for thresholds."""
    return policy.PolicyInput(
        detection_type=detection_type,
        confidence=float(confidence if confidence is not None else 1.0),
        extra=extra or {},
        zone_id=zone_id,
        zone_tags=zone_tags if zone_tags is not None else zone_tags_for(db, zone_id),
        frames_seen=frames_seen,
        min_frames=int(getattr(settings, "temporal_gate_min_frames", 4)),
        has_schedule=store_has_schedule(db, store_id),
        suppressed_reason=suppressed_reason,
        raw_review=raw_review,
        validated_types=frozenset(
            getattr(settings, "policy_validated_detectors", None) or ()),
        min_confidence=float(getattr(settings, "policy_min_confidence", 0.25)),
        safety_min_confidence=float(
            getattr(settings, "policy_safety_min_confidence", 0.5)),
    )


# ------------------------------------------------------------------- attach

@dataclass
class AttachResult:
    incident: Incident
    outcome: str   # opened | appended | renotified | after_close

    @property
    def notify(self) -> bool:
        return self.outcome in ("opened", "renotified")


def _add_member(db: Session, incident: Incident, alert: Alert,
                event: DetectionEvent) -> None:
    db.add(IncidentMember(
        incident_id=incident.id, alert_id=alert.id, event_id=event.id,
        source_event_uuid=str(uuid4()),
        idempotency_key=f"detection-event:{int(event.id)}:v1",
    ))
    alert.incident_id = incident.id


def _transition(db: Session, incident: Incident, to_state: str, *,
                actor_type: str, reason: str, actor_id: str | None = None,
                evidence: dict | None = None, from_state: str | None = None) -> None:
    db.add(IncidentTransition(
        incident_id=incident.id,
        from_state=from_state if from_state is not None else incident.status,
        to_state=to_state, actor_type=actor_type, actor_id=actor_id,
        reason_code=reason[:64], evidence_json=evidence,
    ))


def _open_incident(db: Session, key: str) -> Incident | None:
    return (db.query(Incident)
              .filter(Incident.dedup_key == key,
                      Incident.status.in_(INCIDENT_OPEN_STATUSES))
              .with_for_update()
              .first())


def _append(db: Session, incident: Incident, alert: Alert,
            event: DetectionEvent, decision: policy.PolicyDecision,
            now: datetime, outcome: str) -> AttachResult:
    _add_member(db, incident, alert, event)
    incident.event_count = int(incident.event_count or 0) + 1
    incident.last_seen_at = now
    incident.version = int(incident.version or 1) + 1
    # Stronger evidence upgrades the incident; it never silently downgrades.
    if (_PRIORITY_RANK.get(decision.priority, 0)
            > _PRIORITY_RANK.get(incident.priority, 0)):
        incident.priority = decision.priority
    if (decision.decision == policy.INCIDENT and incident.queue == "review"
            and incident.status in INCIDENT_OPEN_STATUSES):
        incident.queue = "incident"
        incident.policy_decision = decision.decision
        incident.policy_reason = decision.reason[:96]
        _transition(db, incident, incident.status, actor_type="system",
                    reason="promoted_from_review",
                    evidence={"alert_id": alert.id, "reason": decision.reason})
        outcome = "renotified"
    if outcome == "renotified":
        incident.notify_count = int(incident.notify_count or 0) + 1
    else:
        # Folded evidence: kept for audit, never independently actionable.
        alert.notification_suppressed = True
    if incident.status in INCIDENT_CLOSED_STATUSES:
        # Late evidence on a closed incident is audit only.
        alert.status = incident.status
        alert.acknowledged_at = alert.acknowledged_at or now
    db.flush()
    return AttachResult(incident, outcome)


def attach_detection(db: Session, alert: Alert, event: DetectionEvent,
                     decision: policy.PolicyDecision, *,
                     store_id: int | None,
                     now: datetime | None = None) -> AttachResult:
    """Attach one persisted alert to its incident (see module docstring)."""
    if decision.decision not in (policy.INCIDENT, policy.REVIEW):
        raise ValueError(f"{decision.decision!r} detections are not incidents")
    now = _aware(now) or datetime.now(timezone.utc)
    rule = _rule_of(event)
    key = dedup_key(store_id=store_id, camera_id=event.camera_id,
                    zone_id=event.zone_id, detection_type=event.detection_type,
                    rule=rule)
    window = window_for(event.detection_type)
    queue = "incident" if decision.decision == policy.INCIDENT else "review"
    if queue == "review":
        alert.review_only = True
        alert.notification_suppressed = True

    for _attempt in range(3):
        current = _open_incident(db, key)
        if current is not None:
            last = _aware(current.last_seen_at) or now
            outcome = ("renotified" if now - last > window
                       and current.status == "open" and queue == "incident"
                       else "appended")
            return _append(db, current, alert, event, decision, now, outcome)

        recent_closed = (
            db.query(Incident)
              .filter(Incident.dedup_key == key,
                      Incident.status.in_(INCIDENT_CLOSED_STATUSES),
                      Incident.last_seen_at >= now - window)
              .order_by(Incident.last_seen_at.desc())
              .first())
        if recent_closed is not None and decision.priority != policy.URGENT:
            return _append(db, recent_closed, alert, event, decision, now,
                           "after_close")

        try:
            with db.begin_nested():
                incident = Incident(
                    incident_key=f"inc:{uuid4().hex}",
                    camera_id=event.camera_id, store_id=store_id,
                    zone_id=event.zone_id,
                    detection_type=event.detection_type, rule=rule,
                    severity=str((event.extra or {}).get("priority") or "info")[:16],
                    status="open", queue=queue, priority=decision.priority,
                    dedup_key=key, first_seen_at=now, last_seen_at=now,
                    opened_at=now, event_count=1, notify_count=1,
                    head_alert_id=alert.id,
                    policy_decision=decision.decision,
                    policy_reason=decision.reason[:96],
                )
                db.add(incident)
                db.flush()
        except IntegrityError:
            # Another worker opened the same incident between our read and
            # insert; loop to append to the winner.
            log.info("incident race on %s; appending to the winner", key)
            continue
        _add_member(db, incident, alert, event)
        _transition(db, incident, "open", actor_type="system",
                    reason=decision.reason, from_state="",
                    evidence={"alert_id": alert.id, "event_id": event.id,
                              "policy": decision.as_dict()})
        db.flush()
        return AttachResult(incident, "opened")
    raise RuntimeError(f"could not attach alert {alert.id} to incident {key}")


def promote(db: Session, alert: Alert, event: DetectionEvent,
            decision: policy.PolicyDecision, *,
            store_id: int | None) -> AttachResult | None:
    """Production entry point: attach in a savepoint, never lose the alert.

    Quality-control quarantine (set on the alert just before this call)
    downgrades an incident to review. Any failure rolls back only the
    incident rows; the alert stays, unattached, and the periodic
    ``incidents.attach_orphans`` sweep folds it in later, so a transient
    error can delay an incident but never hide one.
    """
    if decision.decision not in (policy.INCIDENT, policy.REVIEW):
        return None
    if alert.review_only and decision.decision == policy.INCIDENT:
        decision = policy.PolicyDecision(
            policy.REVIEW, "quality_control_review_only", policy.LOW)
    try:
        with db.begin_nested():
            return attach_detection(db, alert, event, decision,
                                    store_id=store_id)
    except Exception as exc:
        log.error("incident attach failed alert=%s event=%s: %s; alert kept "
                  "for the orphan sweep", alert.id, event.id, exc)
        return None


def close_store_not_opened(db: Session, *, store_id: int | None,
                           evidence_event_id: int | None,
                           now: datetime | None = None) -> int:
    """First valid entry closes an open "store not opened" incident."""
    if store_id is None:
        return 0
    now = _aware(now) or datetime.now(timezone.utc)
    key = dedup_key(store_id=store_id, camera_id=None, zone_id=None,
                    detection_type="shop_open_close", rule="shop_not_opened")
    closed = 0
    for inc in (db.query(Incident)
                  .filter(Incident.dedup_key == key,
                          Incident.status.in_(INCIDENT_OPEN_STATUSES))
                  .with_for_update().all()):
        prev = inc.status
        inc.status = "resolved"
        inc.closed_at = now
        inc.close_reason = "first_valid_entry"
        inc.version = int(inc.version or 1) + 1
        _transition(db, inc, "resolved", actor_type="system",
                    reason="first_valid_entry", from_state=prev,
                    evidence={"event_id": evidence_event_id})
        _close_member_alerts(db, inc, "resolved", now, actor_user_id=None)
        closed += 1
    db.flush()
    return closed


# ------------------------------------------------------------------ actions

class IncidentActionError(ValueError):
    pass


def _close_member_alerts(db: Session, incident: Incident, status: str,
                         now: datetime, *, actor_user_id: int | None) -> None:
    for a in (db.query(Alert)
                .filter(Alert.incident_id == incident.id,
                        Alert.status.in_(("new", "escalated")))
                .all()):
        a.status = status
        a.acknowledged_at = a.acknowledged_at or now
        if status == "resolved":
            a.resolved_at = now
        if actor_user_id is not None:
            a.assigned_to = actor_user_id


def apply_action(db: Session, incident: Incident, action: str, *,
                 actor_user_id: int, reason: str | None = None,
                 expected_version: int | None = None,
                 now: datetime | None = None) -> Incident:
    """Acknowledge / resolve / dismiss / escalate the WHOLE incident.

    Does not commit. Training is not touched here: verdict evidence is
    recorded separately and enters training only through review
    (app.services.training_evidence).
    """
    if action not in ACTIONS:
        raise IncidentActionError(f"unknown action {action!r}")
    if expected_version is not None and int(expected_version) != int(incident.version):
        raise IncidentActionError("incident changed since it was loaded")
    now = _aware(now) or datetime.now(timezone.utc)
    prev = incident.status
    if prev in INCIDENT_CLOSED_STATUSES and action != "escalate":
        raise IncidentActionError(f"incident is already {prev}")

    if action == "acknowledge":
        if incident.acknowledged_at is None:
            incident.acknowledged_at = now
        if prev == "open":
            incident.status = "acknowledged"
        for a in db.query(Alert).filter(Alert.incident_id == incident.id,
                                        Alert.acknowledged_at.is_(None)).all():
            a.acknowledged_at = now
    elif action == "escalate":
        incident.status = "escalated"
        incident.escalated_at = now
        incident.acknowledged_at = incident.acknowledged_at or now
        incident.closed_at = None
        for a in db.query(Alert).filter(Alert.incident_id == incident.id,
                                        Alert.status == "new").all():
            a.status = "escalated"
            a.acknowledged_at = a.acknowledged_at or now
    else:  # resolve / dismiss
        status = "resolved" if action == "resolve" else "dismissed"
        incident.status = status
        incident.acknowledged_at = incident.acknowledged_at or now
        incident.closed_at = now
        incident.closed_by = actor_user_id
        incident.close_reason = (reason or action)[:64]
        _close_member_alerts(db, incident, status, now,
                             actor_user_id=actor_user_id)
        head = db.get(Alert, incident.head_alert_id) if incident.head_alert_id else None
        if head is not None and action == "dismiss" and reason:
            head.dismiss_reason = reason[:32]
    incident.version = int(incident.version or 1) + 1
    _transition(db, incident, incident.status, actor_type="operator",
                actor_id=str(actor_user_id), reason=reason or action,
                from_state=prev)
    db.flush()
    return incident


# ------------------------------------------------------------------ queries

def incident_query(db: Session, *, view: str = "all",
                   store_id: int | None = None, camera_id: int | None = None,
                   detection_type: str | None = None,
                   since: datetime | None = None,
                   until: datetime | None = None) -> Query:
    """The ONE query behind lists, counts, summary, export and bulk actions.

    Date range: an incident is in range when its activity overlaps
    [since, until) — opened before ``until`` and last seen at/after
    ``since``. An incident opened yesterday that is still active today is
    therefore in today's view.
    """
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}")
    q = db.query(Incident).filter(Incident.queue.in_(("incident", "review")))
    if store_id is not None:
        q = q.filter(Incident.store_id == store_id)
    if camera_id is not None:
        q = q.filter(Incident.camera_id == camera_id)
    if detection_type:
        q = q.filter(Incident.detection_type == detection_type)
    if until is not None:
        q = q.filter(Incident.opened_at < until)
    if since is not None:
        q = q.filter(or_(Incident.last_seen_at >= since,
                         and_(Incident.last_seen_at.is_(None),
                              Incident.opened_at >= since)))
    is_open = Incident.status.in_(INCIDENT_OPEN_STATUSES)
    if view == "open":
        q = q.filter(Incident.queue == "incident", is_open)
    elif view == "attention":
        q = q.filter(Incident.queue == "incident", is_open,
                     Incident.priority != "urgent")
    elif view == "review":
        q = q.filter(Incident.queue == "review", is_open)
    elif view == "resolved":
        q = q.filter(Incident.status.in_(INCIDENT_CLOSED_STATUSES))
    return q


def summarize(db: Session, **filters) -> dict:
    """Counts for the summary bar and every tab, from incident_query."""
    base = incident_query(db, view="all", **filters)
    rows = (base.with_entities(Incident.queue, Incident.status,
                               Incident.priority, func.count(Incident.id))
                .group_by(Incident.queue, Incident.status, Incident.priority)
                .all())
    urgent = attention = review = resolved = total = 0
    for queue, status, priority, n in rows:
        total += n
        if status in INCIDENT_CLOSED_STATUSES:
            resolved += n
        elif queue == "review":
            review += n
        elif priority == "urgent":
            urgent += n
        else:
            attention += n
    # Response time: open -> first operator touch, for incidents in range.
    touched = (base.with_entities(Incident.opened_at, Incident.acknowledged_at,
                                  Incident.closed_at)
                   .filter(Incident.queue == "incident",
                           or_(Incident.acknowledged_at.isnot(None),
                               Incident.closed_at.isnot(None)))
                   .all())
    secs = []
    for opened, acked, closed in touched:
        first = _aware(acked) or _aware(closed)
        if first and opened:
            secs.append(max(0.0, (first - _aware(opened)).total_seconds()))
    return {
        "urgent": urgent,
        "attention": attention,
        "review": review,
        "resolved": resolved,
        "open": urgent + attention,
        "all": total,
        "avg_response_seconds": (round(sum(secs) / len(secs)) if secs else None),
    }


def bulk_resolve(db: Session, *, view: str, expected_count: int,
                 actor_user_id: int, reason: str = "bulk_resolve",
                 **filters) -> list[int]:
    """Resolve exactly the incidents the operator confirmed.

    The open incidents matching the same query the page shows are locked
    and counted; if that count is not ``expected_count`` nothing changes
    and IncidentActionError is raised, so the confirmation dialog can
    never under-state what will be closed.
    """
    if view not in ("open", "attention", "review", "all"):
        raise IncidentActionError("bulk resolve works on open views only")
    q = (incident_query(db, view=view, **filters)
           .filter(Incident.status.in_(INCIDENT_OPEN_STATUSES))
           .order_by(Incident.id)
           .with_for_update())
    targets = q.all()
    if len(targets) != int(expected_count):
        raise IncidentActionError(
            f"expected {expected_count} incidents but {len(targets)} match now")
    now = datetime.now(timezone.utc)
    for inc in targets:
        apply_action(db, inc, "resolve", actor_user_id=actor_user_id,
                     reason=reason, now=now)
    return [inc.id for inc in targets]


# ---------------------------------------------------------------- backfill

def backfill_open_alerts(db: Session, *, since: datetime,
                         limit: int = 5000) -> dict:
    """Fold legacy open alerts created since ``since`` into incidents.

    Used once when the lifecycle is switched on, so alerts raised just
    before the switch are not stranded outside the new Open Incidents
    view. Alerts are processed oldest first, through the same policy and
    attach path as live detections. Caller commits.
    """
    from app.models import Camera

    from app.services.alert_policy import COVERAGE_TYPES, INFORMATIONAL_TYPES

    rows = (db.query(Alert, DetectionEvent)
              .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
              .filter(Alert.incident_id.is_(None),
                      Alert.status == "new",
                      Alert.created_at >= since,
                      DetectionEvent.detection_type.notin_(
                          tuple(INFORMATIONAL_TYPES | COVERAGE_TYPES)))
              .order_by(Alert.created_at, Alert.id)
              .limit(max(1, int(limit)))
              .all())
    stats = {"seen": len(rows), "opened": 0, "appended": 0, "skipped": 0}
    store_cache: dict[int, int | None] = {}
    for alert, event in rows:
        if event.camera_id not in store_cache:
            store_cache[event.camera_id] = db.query(Camera.store_id).filter(
                Camera.id == event.camera_id).scalar()
        stamped = (event.extra or {}).get("policy") or {}
        if stamped.get("decision") in policy.DECISIONS:
            # The live path already decided, with frame context we no
            # longer have; honour it.
            decision = policy.PolicyDecision(
                stamped["decision"], str(stamped.get("reason") or "stamped"),
                str(stamped.get("priority") or policy.ATTENTION))
        else:
            decision = policy.decide(build_policy_input(
                db, detection_type=event.detection_type,
                confidence=event.confidence, extra=event.extra,
                zone_id=event.zone_id, store_id=store_cache[event.camera_id]))
        if alert.review_only and decision.decision == policy.INCIDENT:
            decision = policy.PolicyDecision(
                policy.REVIEW, "quality_control_review_only", policy.LOW)
        if decision.decision not in (policy.INCIDENT, policy.REVIEW):
            stats["skipped"] += 1
            continue
        res = attach_detection(db, alert, event, decision,
                               store_id=store_cache[event.camera_id],
                               now=_aware(event.timestamp) or _aware(alert.created_at))
        stats["opened" if res.outcome == "opened" else "appended"] += 1
    db.flush()
    return stats
