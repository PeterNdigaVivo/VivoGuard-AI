"""Governed operations-assurance and Odoo/event-fusion API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import (
    Annotation, AssuranceCase, Camera, CriticalZoneRequirement, Dataset,
    DetectionEvent, GovernanceAuditLog, OperationalEvent, RiskReview, Store,
    TrainingImage,
)
from app.integrations.odoo_pos import normalise_odoo_event
from app.operations.assurance import (
    DELIVERY_EVENT_TYPES, POS_EVENT_TYPES, assess_coverage, correlate_event,
    create_alert_quality_cases, create_lone_worker_cases, upsert_case,
)

router = APIRouter(prefix="/operations", tags=["operations-assurance"])


class RequirementIn(BaseModel):
    store_id: int
    name: str = Field(min_length=2, max_length=128)
    zone_kind: str = Field(min_length=2, max_length=32)
    zone_id: int | None = None
    required_camera_count: int = Field(default=1, ge=1, le=8)
    max_frame_age_seconds: int = Field(default=120, ge=15, le=3600)
    requires_incident_clip: bool = True


class MissedEventIn(BaseModel):
    source: str = Field(default="manual", max_length=32)
    source_ref: str = Field(max_length=128)
    store_id: int
    camera_id: int | None = None
    occurred_at: datetime
    report_text: str = Field(min_length=4, max_length=4000)
    label: str = Field(min_length=2, max_length=64)
    evidence_path: str | None = Field(default=None, max_length=1000)
    bbox_yolo: list[float] | None = Field(default=None, min_length=4, max_length=4)
    match_window_seconds: int = Field(default=120, ge=15, le=1800)


class OperationalEventIn(BaseModel):
    source: str = Field(default="odoo", max_length=32)
    source_event_id: str = Field(min_length=1, max_length=128)
    store_id: int
    event_type: str = Field(max_length=40)
    occurred_at: datetime
    amount: float | None = None
    currency: str | None = Field(default=None, max_length=8)
    actor_ref: str | None = Field(default=None, max_length=128)
    transaction_ref: str | None = Field(default=None, max_length=128)
    payload: dict | None = None


class ReviewIn(BaseModel):
    conclusion: str = Field(min_length=3, max_length=4000)
    status: str = Field(pattern="^(cleared|action_required|insufficient_evidence)$")


class CaseResolutionIn(BaseModel):
    resolution: str = Field(min_length=3, max_length=4000)
    status: str = Field(pattern="^(resolved|monitoring|insufficient_evidence)$")


def _audit(db: Session, user, action: str, entity_type: str, entity_id, details: dict | None = None):
    db.add(GovernanceAuditLog(actor_user_id=user.id, actor_email=user.email,
                              action=action, entity_type=entity_type,
                              entity_id=str(entity_id), details=details))


def _pseudonym(value: str | None, source: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(f"vivoguard:{source}:{value}".encode()).hexdigest()


def _safe_payload(payload: dict | None) -> dict | None:
    if not payload:
        return payload
    blocked = {"employee_name", "customer_name", "email", "phone", "biometric", "face_embedding"}
    return {key: value for key, value in payload.items() if key.lower() not in blocked}


@router.post("/coverage/requirements")
def create_requirement(body: RequirementIn, db: Session = Depends(get_db),
                       user=Depends(require_role("admin", "operator"))):
    if not db.get(Store, body.store_id):
        raise HTTPException(404, "store not found")
    row = CriticalZoneRequirement(**body.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "a requirement with this store and name already exists")
    _audit(db, user, "coverage_requirement.created", "critical_zone_requirement", row.id, body.model_dump(mode="json"))
    db.commit()
    return {"id": row.id, "status": "configured", "deployment_state": "database_configuration"}


@router.get("/coverage")
def coverage(persist: bool = False, db: Session = Depends(get_db),
             _user=Depends(get_current_user)):
    results = assess_coverage(db, persist=persist)
    if persist:
        db.commit()
    return {"requirements": len(results), "results": results,
            "note": "A pass is based on configured requirements and current telemetry; it is not proof of deployment."}


@router.post("/missed-events")
def report_missed_event(body: MissedEventIn, db: Session = Depends(get_db),
                        user=Depends(require_role("admin", "operator"))):
    store = db.get(Store, body.store_id)
    if not store:
        raise HTTPException(404, "store not found")
    occurred = body.occurred_at if body.occurred_at.tzinfo else body.occurred_at.replace(tzinfo=timezone.utc)
    q = db.query(DetectionEvent).join(Camera, Camera.id == DetectionEvent.camera_id).filter(
        Camera.store_id == body.store_id,
        DetectionEvent.timestamp.between(occurred - timedelta(seconds=body.match_window_seconds),
                                         occurred + timedelta(seconds=body.match_window_seconds)))
    if body.camera_id:
        q = q.filter(DetectionEvent.camera_id == body.camera_id)
    matched = q.order_by(DetectionEvent.timestamp.desc()).first()
    camera = db.get(Camera, body.camera_id) if body.camera_id else None
    if matched:
        root_cause, training_status = "alert_or_triage_gap", "labelled_case_matched_to_detection"
    elif camera and (camera.status != "online" or not camera.last_seen_at):
        root_cause, training_status = "camera_unavailable", "blocked_no_visual_evidence"
    elif camera and not camera.zones:
        root_cause, training_status = "zone_unconfigured", "labelled_case_pending_evidence_review"
    else:
        root_cause = "detector_false_negative"
        training_status = "labelled_sample_pending_human_verification" if body.evidence_path else "blocked_no_visual_evidence"
    case = upsert_case(
        db, dedup_key=f"missed:{body.source}:{body.source_ref}", case_type="missed_event",
        severity="high", title=f"Human-reported event: {body.label}",
        description=body.report_text, store_id=body.store_id, camera_id=body.camera_id,
        event_id=matched.id if matched else None, root_cause=root_cause,
        evidence={"source": body.source, "source_ref": body.source_ref,
                  "occurred_at": occurred.isoformat(), "evidence_path": body.evidence_path,
                  "matched_event_id": matched.id if matched else None},
        label_json={"label": body.label, "verified_by_user_id": user.id},
        training_status=training_status,
    )
    db.flush()
    training_image_id = None
    if body.evidence_path and case.training_status not in {
        "verified_labelled_sample_created", "sample_created_pending_bbox_verification"}:
        dataset = db.query(Dataset).filter(Dataset.name == "human_missed_events").first()
        if not dataset:
            dataset = Dataset(name="human_missed_events",
                              description="Human-reported missed events; evidence requires verification before training.",
                              classes_json=[body.label])
            db.add(dataset)
            db.flush()
        elif body.label not in (dataset.classes_json or []):
            dataset.classes_json = [*(dataset.classes_json or []), body.label]
        image = TrainingImage(dataset_id=dataset.id, camera_id=body.camera_id,
                              file_path=body.evidence_path, captured_at=occurred,
                              labeled=bool(body.bbox_yolo),
                              source_extra={"assurance_case_id": case.id, "target_label": body.label,
                                            "source": body.source, "human_review_required": True})
        db.add(image)
        db.flush()
        training_image_id = image.id
        if body.bbox_yolo:
            db.add(Annotation(image_id=image.id, class_label=body.label,
                              bbox_json=body.bbox_yolo, annotated_by=user.id,
                              verified=True, auto_suggested=False))
            case.training_status = "verified_labelled_sample_created"
        else:
            case.training_status = "sample_created_pending_bbox_verification"
    _audit(db, user, "missed_event.reported", "assurance_case", case.id,
           {"root_cause": root_cause, "training_status": case.training_status})
    db.commit()
    return {"case_id": case.id, "root_cause": root_cause,
            "training_status": case.training_status, "training_image_id": training_image_id,
            "matched_event_id": matched.id if matched else None}


def _ingest_event(body: OperationalEventIn, db: Session, user):
    allowed = POS_EVENT_TYPES | DELIVERY_EVENT_TYPES
    if body.event_type not in allowed:
        raise HTTPException(422, f"event_type must be one of: {', '.join(sorted(allowed))}")
    if not db.get(Store, body.store_id):
        raise HTTPException(404, "store not found")
    existing = db.query(OperationalEvent).filter(
        OperationalEvent.source == body.source,
        OperationalEvent.source_event_id == body.source_event_id).one_or_none()
    if existing:
        return {"id": existing.id, "duplicate": True, "risk_review_id": None}
    event_data = body.model_dump()
    event_data["actor_ref"] = _pseudonym(body.actor_ref, body.source)
    event_data["payload"] = _safe_payload(body.payload)
    event = OperationalEvent(**event_data)
    db.add(event)
    db.flush()
    review = correlate_event(db, event)
    db.flush()
    _audit(db, user, "operational_event.ingested", "operational_event", event.id,
           {"source": event.source, "event_type": event.event_type, "risk_review_id": review.id})
    db.commit()
    return {"id": event.id, "duplicate": False, "risk_review_id": review.id,
            "review_band": review.band, "human_review_required": True,
            "wording": "Priority for review only; this is not an accusation or finding."}


@router.post("/events")
def ingest_event(body: OperationalEventIn, db: Session = Depends(get_db),
                 user=Depends(require_role("admin", "operator"))):
    return _ingest_event(body, db, user)


@router.post("/events/odoo")
def ingest_odoo_event(payload: dict, db: Session = Depends(get_db),
                      user=Depends(require_role("admin", "operator"))):
    try:
        body = OperationalEventIn(**normalise_odoo_event(payload))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _ingest_event(body, db, user)


@router.get("/cases")
def list_cases(status: str | None = None, case_type: str | None = None,
               limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db),
               _user=Depends(get_current_user)):
    q = db.query(AssuranceCase)
    if status:
        q = q.filter(AssuranceCase.status == status)
    if case_type:
        q = q.filter(AssuranceCase.case_type == case_type)
    return q.order_by(AssuranceCase.first_seen_at.desc()).limit(limit).all()


@router.post("/cases/{case_id}/resolve")
def resolve_case(case_id: int, body: CaseResolutionIn, db: Session = Depends(get_db),
                 user=Depends(require_role("admin", "operator"))):
    row = db.get(AssuranceCase, case_id)
    if not row:
        raise HTTPException(404, "assurance case not found")
    row.status, row.resolution = body.status, body.resolution
    row.reviewed_by, row.reviewed_at = user.id, datetime.now(timezone.utc)
    if body.status == "resolved":
        row.resolved_at = row.reviewed_at
    _audit(db, user, "assurance_case.reviewed", "assurance_case", row.id,
           {"status": body.status})
    db.commit()
    return {"id": row.id, "status": row.status, "human_review_completed": True}


@router.post("/assess-now")
def assess_now(db: Session = Depends(get_db), user=Depends(require_role("admin", "operator"))):
    coverage_results = assess_coverage(db, persist=True)
    alert_cases = create_alert_quality_cases(db)
    lone_worker_cases = create_lone_worker_cases(db)
    _audit(db, user, "assurance.assess_now", "system", "operations",
           {"coverage_requirements": len(coverage_results), "alert_cases": alert_cases,
            "lone_worker_cases": lone_worker_cases})
    db.commit()
    return {"coverage_requirements": len(coverage_results), "alert_quality_cases": alert_cases,
            "lone_worker_cases": lone_worker_cases}


@router.get("/risk-reviews")
def list_risk_reviews(status: str | None = None, limit: int = Query(200, le=1000),
                      db: Session = Depends(get_db), _user=Depends(get_current_user)):
    q = db.query(RiskReview)
    if status:
        q = q.filter(RiskReview.status == status)
    return q.order_by(RiskReview.created_at.desc()).limit(limit).all()


@router.post("/risk-reviews/{review_id}/review")
def review_risk(review_id: int, body: ReviewIn, db: Session = Depends(get_db),
                user=Depends(require_role("admin", "operator"))):
    row = db.get(RiskReview, review_id)
    if not row:
        raise HTTPException(404, "risk review not found")
    row.status, row.conclusion = body.status, body.conclusion
    row.reviewed_by, row.reviewed_at = user.id, datetime.now(timezone.utc)
    _audit(db, user, "risk_review.completed", "risk_review", row.id,
           {"status": body.status})
    db.commit()
    return {"id": row.id, "status": row.status, "human_review_completed": True}
