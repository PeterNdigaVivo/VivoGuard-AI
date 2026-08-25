"""Deterministic, evidence-first risk assessment helpers.

These functions create review work; they never make employment, criminal or
disciplinary conclusions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import (
    Alert, AssuranceCase, Camera, CriticalZoneRequirement, DetectionEvent,
    OperationalEvent, RecordingClip, RiskReview, Store, Zone,
)
from app.risk.scoring import risk_band, score_operational_event

OPEN_STATUSES = ("open", "investigating", "pending_human_review")
POS_EVENT_TYPES = {"refund", "void", "discount", "no_sale", "high_value_transaction"}
DELIVERY_EVENT_TYPES = {"delivery_expected", "delivery_received", "stock_move", "stock_exit"}
ACTIONABLE_SHOP_RULES = {
    "shop_not_opened", "shop_opened_before_hours", "shop_opened_late",
}
ACTIONABLE_SALES_FLOOR_RULES = {
    "low_engagement", "unattended_floor", "detection_offline",
}


def _requires_operator_action(event: DetectionEvent) -> bool:
    """Exclude routine updates from acknowledgement/evidence SLAs.

    The alert feed intentionally retains positive and informational records,
    but those records are not incidents awaiting control-room disposition.
    Rule-specific detectors override their legacy priority stamp because
    older sales-floor alerts labelled every rule as ``info``.
    """
    extra = event.extra or {}
    detection_type = str(event.detection_type or "")
    rule = str(extra.get("rule") or extra.get("cls") or "").lower()
    if detection_type in {"store_intelligence", "positive_operational"}:
        return False
    if detection_type == "shop_open_close":
        return rule in ACTIONABLE_SHOP_RULES
    if detection_type == "sales_floor_insight":
        return rule in ACTIONABLE_SALES_FLOOR_RULES
    if detection_type == "live_activity":
        return rule != "activity_presence"
    priority = str(extra.get("priority") or "").lower()
    return priority not in {"info", "positive"}


def _actionable_event_filter():
    """SQL equivalent of :func:`_requires_operator_action` for backlog counts."""
    priority = func.lower(func.coalesce(
        DetectionEvent.extra["priority"].as_string(), "",
    ))
    rule = func.lower(func.coalesce(
        DetectionEvent.extra["rule"].as_string(),
        DetectionEvent.extra["cls"].as_string(),
        "",
    ))
    detection_type = DetectionEvent.detection_type
    return and_(
        detection_type.notin_(("store_intelligence", "positive_operational")),
        or_(
            and_(detection_type == "shop_open_close",
                 rule.in_(ACTIONABLE_SHOP_RULES)),
            and_(detection_type == "sales_floor_insight",
                 rule.in_(ACTIONABLE_SALES_FLOOR_RULES)),
            and_(detection_type == "live_activity",
                 rule != "activity_presence"),
            and_(
                detection_type.notin_((
                    "shop_open_close", "sales_floor_insight", "live_activity",
                )),
                priority.notin_(("info", "positive")),
            ),
        ),
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _alert_has_retrievable_evidence(
    alert: Alert, event: DetectionEvent,
) -> bool:
    """Return whether any canonical alert evidence is still viewable.

    Recorder clips were historically written to ``event.extra`` while newer
    detectors may use ``event.clip_path``. Immediate snapshots live on the
    event and filmstrips live on the alert. A stored path is not sufficient:
    retention may already have removed the file, so assurance must verify the
    filesystem before declaring evidence available.
    """
    candidates = [
        event.clip_path,
        (event.extra or {}).get("alert_clip_path"),
        event.thumbnail_path,
        *(alert.snapshot_paths or []),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(str(candidate)).is_file():
                return True
        except (OSError, TypeError, ValueError):
            continue
    return False


def store_is_open(store: Store, at: datetime) -> bool:
    if not store.business_hours_json:
        return True
    local = ensure_aware(at).astimezone(ZoneInfo(store.timezone or "Africa/Nairobi"))
    keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for window in store.business_hours_json.get(keys[local.weekday()], []):
        try:
            start, end = window.split("-", 1)
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
            minutes = local.hour * 60 + local.minute
            if sh * 60 + sm <= minutes <= eh * 60 + em:
                return True
        except (TypeError, ValueError):
            continue
    return False


def upsert_case(db: Session, *, dedup_key: str, case_type: str, severity: str,
                title: str, store_id: int | None = None, camera_id: int | None = None,
                zone_id: int | None = None, alert_id: int | None = None,
                event_id: int | None = None, description: str | None = None,
                root_cause: str | None = None, evidence: dict | None = None,
                label_json: dict | None = None, training_status: str | None = None) -> AssuranceCase:
    row = db.query(AssuranceCase).filter(AssuranceCase.dedup_key == dedup_key).one_or_none()
    now = utc_now()
    if row:
        row.last_seen_at = now
        row.evidence = evidence
        if row.status == "resolved":
            row.status, row.resolved_at = "open", None
        return row
    row = AssuranceCase(
        dedup_key=dedup_key, case_type=case_type, severity=severity, title=title,
        description=description, store_id=store_id, camera_id=camera_id,
        zone_id=zone_id, alert_id=alert_id, event_id=event_id,
        root_cause=root_cause, evidence=evidence, label_json=label_json,
        training_status=training_status, human_review_required=True,
    )
    db.add(row)
    return row


def _resolve_inactive_cases(db: Session, *, case_type: str,
                            active_keys: set[str], resolution: str) -> None:
    """Close previously-open exceptions that the latest assessment cleared."""
    now = utc_now()
    rows = db.query(AssuranceCase).filter(
        AssuranceCase.case_type == case_type,
        AssuranceCase.status.in_(OPEN_STATUSES),
    ).all()
    for row in rows:
        if row.dedup_key in active_keys:
            continue
        row.status = "resolved"
        row.resolved_at = now
        row.resolution = resolution


def assess_coverage(db: Session, now: datetime | None = None, *, persist: bool = False) -> list[dict]:
    now = ensure_aware(now or utc_now())
    results: list[dict] = []
    active_case_keys: set[str] = set()
    requirements = db.query(CriticalZoneRequirement).filter(
        CriticalZoneRequirement.is_active.is_(True)).all()
    configured_store_ids = {r.store_id for r in requirements}
    for store in db.query(Store).filter(Store.is_active.is_(True)).all():
        if store.id in configured_store_ids:
            continue
        item = {"requirement_id": None, "store_id": store.id, "name": store.name,
                "zone_kind": None, "status": "critical",
                "issues": ["critical_zone_requirements_not_configured"],
                "camera_ids": [], "fresh_camera_ids": [], "clip_retrievable": False,
                "assessed_at": now.isoformat()}
        results.append(item)
        if persist:
            key = f"coverage-config:{store.id}"
            active_case_keys.add(key)
            upsert_case(db, dedup_key=key, case_type="coverage_gap",
                        severity="critical", title=f"Critical-zone map missing: {store.name}",
                        store_id=store.id, evidence=item,
                        description="Map entrance, POS/cash, sales floor and stockroom/back-door zones to cameras.")
    for req in requirements:
        cameras_q = db.query(Camera).filter(
            Camera.store_id == req.store_id, Camera.is_deleted.is_(False))
        if req.zone_id:
            cameras_q = cameras_q.join(Zone, Zone.camera_id == Camera.id).filter(Zone.id == req.zone_id)
        cameras = cameras_q.all()
        fresh = [c for c in cameras if c.status == "online" and c.last_seen_at and
                 now - ensure_aware(c.last_seen_at) <= timedelta(seconds=req.max_frame_age_seconds)]
        clip_ids = set()
        if fresh and req.requires_incident_clip:
            clip_ids = {cid for (cid,) in db.query(RecordingClip.camera_id).filter(
                RecordingClip.camera_id.in_([c.id for c in fresh]),
                RecordingClip.status.in_(("recording", "completed")),
                RecordingClip.file_path.is_not(None)).distinct().all()}
        clip_ok = (not req.requires_incident_clip or any(c.id in clip_ids for c in fresh))
        issues = []
        if len(fresh) < req.required_camera_count:
            issues.append("insufficient_fresh_cameras")
        if req.zone_id and not db.get(Zone, req.zone_id):
            issues.append("zone_missing")
        if not clip_ok:
            issues.append("incident_clip_unavailable")
        status = "critical" if "insufficient_fresh_cameras" in issues else ("warning" if issues else "pass")
        item = {"requirement_id": req.id, "store_id": req.store_id, "name": req.name,
                "zone_kind": req.zone_kind, "status": status, "issues": issues,
                "camera_ids": [c.id for c in cameras], "fresh_camera_ids": [c.id for c in fresh],
                "clip_retrievable": clip_ok, "assessed_at": now.isoformat()}
        results.append(item)
        if persist and issues:
            key = f"coverage:{req.id}:{','.join(sorted(issues))}"
            active_case_keys.add(key)
            upsert_case(db, dedup_key=key,
                        case_type="coverage_gap", severity=status,
                        title=f"Coverage assurance failed: {req.name}", store_id=req.store_id,
                        zone_id=req.zone_id, evidence=item,
                        description="Restore coverage or correct the mapped camera/zone; verify a retrievable clip.")
    if persist:
        _resolve_inactive_cases(
            db, case_type="coverage_gap", active_keys=active_case_keys,
            resolution="Latest coverage assessment confirms that this exception has cleared.",
        )
    return results


def create_alert_quality_cases(db: Session, now: datetime | None = None) -> int:
    now = ensure_aware(now or utc_now())
    review_cutoff = now - timedelta(days=7)
    count = 0
    rows = (db.query(Alert, DetectionEvent, Camera)
            .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
            .join(Camera, Camera.id == DetectionEvent.camera_id)
            .filter(Alert.status.in_(("new", "escalated")),
                    Alert.resolved_at.is_(None),
                    Alert.created_at >= review_cutoff,
                    DetectionEvent.detection_type != "store_intelligence")
            .order_by(Alert.created_at.desc()).limit(500).all())
    critical_types = {"weapon", "weapon_brandished", "brandished_weapon",
                      "fight", "fire", "smoke", "intrusion"}
    active_case_keys: set[str] = set()
    assessed_alert_ids = {int(alert.id) for alert, _event, _camera in rows}
    for alert, event, camera in rows:
        if not _requires_operator_action(event):
            continue
        age = (now - ensure_aware(alert.created_at)).total_seconds()
        sla = 300 if event.detection_type in critical_types else 1800
        latency = (ensure_aware(alert.created_at) - ensure_aware(event.timestamp)).total_seconds()
        issues = []
        if not alert.acknowledged_at and age > sla:
            issues.append("acknowledgement_sla_breached")
            if alert.review_only:
                issues.append("quality_controlled_review_sla_breached")
        if latency > 120:
            issues.append("delivery_latency_over_120s")
        if (event.detection_type not in {"system_health", "camera_offline"}
                and not _alert_has_retrievable_evidence(alert, event)):
            issues.append("evidence_missing")
        if issues:
            key = f"alert-quality:{alert.id}:{','.join(sorted(issues))}"
            active_case_keys.add(key)
            quality = ((event.extra or {}).get("quality_control") or {})
            upsert_case(db, dedup_key=key,
                        case_type="alert_quality", severity="critical" if event.detection_type in critical_types else "high",
                        title=(f"Review-only alert {alert.id} breached human-review SLA"
                               if alert.review_only else
                               f"Alert {alert.id} requires operational follow-up"),
                        description=("Accountable owner: Loss Prevention Operations. "
                                     "Quarantine suppresses escalation but never removes the human-review SLA."
                                     if alert.review_only else None),
                        store_id=camera.store_id,
                        camera_id=camera.id, alert_id=alert.id, event_id=event.id,
                        evidence={"issues": issues, "age_seconds": age,
                                  "delivery_latency_seconds": latency,
                                  "sla_seconds": sla,
                                  "accountable_owner": "Loss Prevention Operations",
                                  "quality_mode": quality.get("mode", "review_only" if alert.review_only else "active")})
            count += 1
    historical_count = (db.query(Alert).join(
        DetectionEvent, DetectionEvent.id == Alert.event_id).filter(
            Alert.status.in_(("new", "escalated")),
            Alert.resolved_at.is_(None),
            Alert.created_at < review_cutoff,
            _actionable_event_filter(),
        ).count())
    historical_key = "alert-quality:historical-backlog"
    if historical_count:
        active_case_keys.add(historical_key)
        upsert_case(
            db, dedup_key=historical_key, case_type="alert_quality",
            severity="high", title="Historical alert-review backlog",
            description=("Review and disposition old alerts in controlled batches; "
                         "do not bulk-label them as true or false without evidence."),
            evidence={"unresolved_actionable_alerts": historical_count,
                      "older_than": review_cutoff.isoformat()},
        )
        count += 1
    # Close cases tied to alerts that operators already handled, including
    # alerts outside the bounded 500-row assessment batch.
    for case in db.query(AssuranceCase).filter(
            AssuranceCase.case_type == "alert_quality",
            AssuranceCase.status.in_(OPEN_STATUSES)).all():
        alert = db.get(Alert, case.alert_id) if case.alert_id else None
        alert_open = bool(alert and alert.status in ("new", "escalated") and
                          alert.resolved_at is None)
        in_active_horizon = bool(
            alert_open and alert and ensure_aware(alert.created_at) >= review_cutoff)
        if case.dedup_key in active_case_keys:
            continue
        # The query is deliberately bounded to 500 recent alerts. Preserve
        # cases belonging to open alerts that were outside that assessed
        # batch, but close a case when its assessed condition actually clears.
        if in_active_horizon and int(alert.id) not in assessed_alert_ids:
            continue
        case.status = "resolved"
        case.resolved_at = utc_now()
        case.resolution = (
            "Moved to the governed historical-backlog case."
            if alert_open else
            "Alert is no longer open; the operational exception has cleared."
        )
    return count


def create_lone_worker_cases(db: Session, now: datetime | None = None) -> int:
    now = ensure_aware(now or utc_now())
    since = now - timedelta(minutes=15)
    count = 0
    active_case_keys: set[str] = set()
    for store in db.query(Store).filter(Store.is_active.is_(True)).all():
        if store_is_open(store, now):
            continue
        events = (db.query(DetectionEvent).join(Camera, Camera.id == DetectionEvent.camera_id)
                  .filter(Camera.store_id == store.id, Camera.is_deleted.is_(False),
                          DetectionEvent.detection_type == "person", DetectionEvent.timestamp >= since)
                  .order_by(DetectionEvent.timestamp.desc()).limit(50).all())
        track_ids = {str((e.extra or {}).get("track_id")) for e in events if (e.extra or {}).get("track_id") is not None}
        estimated_people = len(track_ids) or (1 if events else 0)
        if estimated_people == 1:
            evidence = {"event_ids": [e.id for e in events[:10]], "window_minutes": 15,
                        "estimated_people": estimated_people, "basis": "single tracked person after business hours"}
            try:
                from app.config import settings
                from app.operations.odoo_assurance import roster_advisory
                evidence.update(roster_advisory(
                    db, store.id, now,
                    max_age_hours=settings.odoo_roster_max_age_hours))
            except Exception:
                # External context is fail-soft and never blocks the case.
                evidence.update({"expected_staff_window": "unknown",
                                 "alert_suppressed": False})
            key = f"lone-worker:{store.id}:{now.date().isoformat()}"
            active_case_keys.add(key)
            upsert_case(db, dedup_key=key,
                        case_type="lone_worker", severity="high",
                        title=f"Possible lone worker or late departure: {store.name}", store_id=store.id,
                        camera_id=events[0].camera_id, event_id=events[0].id, evidence=evidence,
                        description="Confirm staff authorisation and welfare. Do not infer misconduct from presence alone.")
            count += 1
    _resolve_inactive_cases(
        db, case_type="lone_worker", active_keys=active_case_keys,
        resolution="Latest after-hours assessment no longer shows a single tracked person.",
    )
    return count


def correlate_event(db: Session, event: OperationalEvent) -> RiskReview:
    store = db.get(Store, event.store_id)
    window_start, window_end = event.occurred_at - timedelta(minutes=5), event.occurred_at + timedelta(minutes=5)
    detections = (db.query(DetectionEvent).join(Camera, Camera.id == DetectionEvent.camera_id)
                  .filter(Camera.store_id == event.store_id, DetectionEvent.timestamp.between(window_start, window_end))
                  .order_by(DetectionEvent.timestamp.desc()).limit(20).all())
    evidence = {"detection_event_ids": [d.id for d in detections],
                "clip_event_ids": [d.id for d in detections if d.clip_path],
                "window_seconds": 300}
    after_hours = bool(store and not store_is_open(store, event.occurred_at))
    score, factors = score_operational_event(event.event_type, event.amount,
                                             after_hours=after_hours, camera_evidence=bool(detections))
    review = db.query(RiskReview).filter(RiskReview.operational_event_id == event.id).one_or_none()
    if not review:
        review = RiskReview(operational_event_id=event.id, store_id=event.store_id,
                            risk_type="pos_review" if event.event_type in POS_EVENT_TYPES else "stock_delivery_review",
                            score=score, band=risk_band(score), factors=factors,
                            camera_evidence=evidence, human_review_required=True,
                            status="pending_human_review")
        db.add(review)
    return review
