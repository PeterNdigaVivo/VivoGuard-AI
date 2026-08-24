"""Governed operations-assurance and Odoo/event-fusion API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import (
    Alert, AlertQualityControl, AlertReviewDecision, Annotation, AssuranceCase,
    Camera, CriticalZoneRequirement, Dataset, DetectionEvent,
    GovernanceAuditLog, OperationalEvent, RiskReview, Store, TrainingImage,
)
from app.integrations.odoo_pos import normalise_odoo_event
from app.operations.assurance import (
    DELIVERY_EVENT_TYPES, POS_EVENT_TYPES, assess_coverage, correlate_event,
    create_alert_quality_cases, create_lone_worker_cases, upsert_case,
)
from app.simulation.runner import missing_feedback_fields

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


class DisagreementAdjudicationIn(BaseModel):
    verdict: str = Field(pattern="^(confirm|dismiss|unclear)$")
    rationale: str = Field(min_length=8, max_length=4000)


class FeedbackIntakeIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    urgency: str = Field(default="high", pattern="^(urgent|high|routine)$")
    store: str | None = Field(default=None, max_length=128)
    camera: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None
    observed: str | None = Field(default=None, max_length=2000)
    expected: str | None = Field(default=None, max_length=2000)
    evidence_ref: str | None = Field(default=None, max_length=1000)


class FeedbackClarificationIn(BaseModel):
    response: str = Field(min_length=2, max_length=4000)
    store: str | None = Field(default=None, max_length=128)
    camera: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None
    observed: str | None = Field(default=None, max_length=2000)
    expected: str | None = Field(default=None, max_length=2000)
    evidence_ref: str | None = Field(default=None, max_length=1000)


def _audit(db: Session, user, action: str, entity_type: str, entity_id, details: dict | None = None):
    db.add(GovernanceAuditLog(actor_user_id=getattr(user, "id", None),
                              actor_email=getattr(user, "email", None),
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


def _verify_odoo_signature(raw_body: bytes, timestamp: str, signature: str, *,
                           secret: str | None = None,
                           max_age_seconds: int | None = None,
                           now: datetime | None = None) -> None:
    """Authenticate an Odoo webhook and reject stale/replayed requests."""
    key = secret if secret is not None else settings.odoo_webhook_secret
    if not key:
        raise HTTPException(503, "Odoo webhook credential is not configured")
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "invalid Odoo webhook timestamp") from exc
    current = int((now or datetime.now(timezone.utc)).timestamp())
    max_age = (max_age_seconds if max_age_seconds is not None
               else settings.odoo_webhook_max_age_seconds)
    if abs(current - sent_at) > max_age:
        raise HTTPException(401, "expired Odoo webhook timestamp")
    signed = timestamp.encode() + b"." + raw_body
    expected = "sha256=" + hmac.new(key.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise HTTPException(401, "invalid Odoo webhook signature")


def _clarification_question(missing: list[str]) -> str:
    labels = {"store": "store", "camera": "camera/channel", "occurred_at": "incident time",
              "observed": "what you observed", "expected": "what you expected the system to do"}
    needed = ", ".join(labels[field] for field in missing)
    return f"Please clarify the {needed}, and attach the relevant screenshot or clip if available. I will not classify or train on this report until it is clear."


@router.post("/feedback/intake")
def intake_feedback(body: FeedbackIntakeIn, db: Session = Depends(get_db),
                    user=Depends(require_role("admin", "operator"))):
    payload = body.model_dump(mode="json")
    missing = missing_feedback_fields(payload)
    deadlines = {"urgent": 15, "high": 30, "routine": 120}
    now = datetime.now(timezone.utc)
    question = _clarification_question(missing) if missing else None
    evidence = {**payload, "ambiguity_reasons": missing, "clarification_question": question,
                "clarification_asked_at": now.isoformat() if missing else None,
                "clarification_due_at": (now + timedelta(minutes=deadlines[body.urgency])).isoformat() if missing else None,
                "reminder_count": 0}
    case = upsert_case(db, dedup_key=f"human-feedback:{body.message_id}",
                       case_type="feedback_clarification" if missing else "human_feedback",
                       severity="high" if body.urgency != "routine" else "medium",
                       title="Clarification required for human feedback" if missing else "Human feedback ready for investigation",
                       description=body.observed, evidence=evidence,
                       training_status="blocked_ambiguous_feedback" if missing else "blocked_pending_investigation")
    if missing:
        case.status = "blocked_waiting_human"
    db.flush()
    _audit(db, user, "human_feedback.intake", "assurance_case", case.id,
           {"missing_fields": missing, "message_id": body.message_id})
    db.commit()
    return {"case_id": case.id, "clarification_required": bool(missing),
            "question_to_send_on_whatsapp": question,
            "training_eligible": False, "deadline": evidence["clarification_due_at"]}


@router.post("/feedback/{case_id}/clarify")
def clarify_feedback(case_id: int, body: FeedbackClarificationIn,
                     db: Session = Depends(get_db),
                     user=Depends(require_role("admin", "operator"))):
    case = db.get(AssuranceCase, case_id)
    if not case or case.case_type not in {"feedback_clarification", "human_feedback"}:
        raise HTTPException(404, "human feedback case not found")
    evidence = dict(case.evidence or {})
    for key, value in body.model_dump(mode="json", exclude_none=True).items():
        evidence[key] = value
    missing = missing_feedback_fields(evidence)
    evidence["ambiguity_reasons"] = missing
    evidence["clarified_at"] = datetime.now(timezone.utc).isoformat()
    case.evidence = evidence
    case.status = "blocked_waiting_human" if missing else "investigating"
    case.case_type = "feedback_clarification" if missing else "human_feedback"
    case.training_status = "blocked_ambiguous_feedback" if missing else "blocked_pending_investigation"
    _audit(db, user, "human_feedback.clarified", "assurance_case", case.id,
           {"remaining_missing_fields": missing})
    db.commit()
    return {"case_id": case.id, "clarification_required": bool(missing),
            "remaining_missing_fields": missing, "training_eligible": False}


@router.post("/feedback/{case_id}/reminder")
def record_feedback_reminder(case_id: int, db: Session = Depends(get_db),
                             user=Depends(require_role("admin", "operator"))):
    case = db.get(AssuranceCase, case_id)
    if not case or case.case_type != "feedback_clarification":
        raise HTTPException(404, "feedback clarification case not found")
    evidence = dict(case.evidence or {})
    if int(evidence.get("reminder_count") or 0) >= 1:
        raise HTTPException(409, "one reminder already recorded; escalate ownership instead")
    evidence["reminder_count"] = 1
    evidence["reminder_sent_at"] = datetime.now(timezone.utc).isoformat()
    case.evidence = evidence
    _audit(db, user, "human_feedback.reminder_recorded", "assurance_case", case.id)
    db.commit()
    return {"case_id": case.id, "reminder_count": 1,
            "next_action": "escalate accountable owner if no substantive response"}


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
    camera = db.get(Camera, body.camera_id) if body.camera_id else None
    if body.camera_id and (not camera or camera.is_deleted):
        raise HTTPException(404, "camera not found")
    if camera and camera.store_id != body.store_id:
        raise HTTPException(
            422,
            "camera does not belong to the selected store",
        )
    occurred = body.occurred_at if body.occurred_at.tzinfo else body.occurred_at.replace(tzinfo=timezone.utc)
    q = db.query(DetectionEvent).join(Camera, Camera.id == DetectionEvent.camera_id).filter(
        Camera.store_id == body.store_id,
        DetectionEvent.timestamp.between(occurred - timedelta(seconds=body.match_window_seconds),
                                         occurred + timedelta(seconds=body.match_window_seconds)))
    if body.camera_id:
        q = q.filter(DetectionEvent.camera_id == body.camera_id)
    matched = q.order_by(DetectionEvent.timestamp.desc()).first()
    if matched:
        root_cause, training_status = "alert_or_triage_gap", "labelled_case_matched_to_detection"
    elif camera is None:
        root_cause = "camera_unconfirmed"
        training_status = "blocked_pending_camera_identification"
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
        "labelled_sample_pending_independent_verification",
        "sample_created_pending_bbox_verification",
        "verified_and_eligible_for_training"}:
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
                              source_kind="human_missed_event",
                              eligible_for_training=False,
                              review_state="pending",
                              source_extra={"assurance_case_id": case.id, "target_label": body.label,
                                            "source": body.source, "human_review_required": True})
        db.add(image)
        db.flush()
        training_image_id = image.id
        if body.bbox_yolo:
            db.add(Annotation(image_id=image.id, class_label=body.label,
                              bbox_json=body.bbox_yolo, annotated_by=user.id,
                              verified=False, auto_suggested=False))
            case.training_status = "labelled_sample_pending_independent_verification"
        else:
            case.training_status = "sample_created_pending_bbox_verification"
    _audit(db, user, "missed_event.reported", "assurance_case", case.id,
           {"root_cause": root_cause, "training_status": case.training_status})
    db.commit()
    return {"case_id": case.id, "root_cause": root_cause,
            "training_status": case.training_status, "training_image_id": training_image_id,
            "matched_event_id": matched.id if matched else None}


@router.post("/missed-events/{case_id}/verify-training")
def verify_missed_event_training(case_id: int, db: Session = Depends(get_db),
                                 user=Depends(require_role("admin", "operator"))):
    case = db.get(AssuranceCase, case_id)
    if not case or case.case_type != "missed_event":
        raise HTTPException(404, "missed-event case not found")
    image = (db.query(TrainingImage)
             .filter(TrainingImage.source_extra["assurance_case_id"].as_integer() == case.id)
             .order_by(TrainingImage.id.desc()).first())
    if not image:
        raise HTTPException(409, "no visual training sample is attached")
    if image.eligible_for_training or image.review_state == "approved":
        raise HTTPException(409, "missed-event training evidence is already verified")
    annotations = db.query(Annotation).filter(Annotation.image_id == image.id).all()
    if not annotations:
        raise HTTPException(409, "a reviewed bounding box is required")
    primary_reviewers = {
        reviewer_id for reviewer_id in [
            *(a.annotated_by for a in annotations),
            (case.label_json or {}).get("verified_by_user_id"),
        ] if reviewer_id is not None
    }
    if not primary_reviewers:
        raise HTTPException(409, "primary reviewer provenance is missing")
    if user.id in primary_reviewers:
        raise HTTPException(
            409,
            "missed-event training evidence requires an independent second reviewer",
        )
    for annotation in annotations:
        annotation.verified = True
    image.eligible_for_training = True
    image.review_state = "approved"
    case.training_status = "verified_and_eligible_for_training"
    _audit(db, user, "missed_event.training_verified", "training_image", image.id,
           {"case_id": case.id, "annotation_ids": [a.id for a in annotations]})
    db.commit()
    return {"case_id": case.id, "training_image_id": image.id,
            "eligible_for_training": True, "reviewed_by": user.id}


@router.post("/cases/{case_id}/adjudicate")
def adjudicate_reviewer_disagreement(
    case_id: int,
    body: DisagreementAdjudicationIn,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin", "operator")),
):
    """Resolve a two-reviewer disagreement without erasing either verdict."""
    case = db.get(AssuranceCase, case_id)
    if not case or case.case_type != "reviewer_disagreement":
        raise HTTPException(404, "reviewer-disagreement case not found")
    if case.status != "open":
        raise HTTPException(409, "reviewer-disagreement case is not open")
    if case.alert_id is None:
        raise HTTPException(409, "disagreement case is not linked to an alert")
    alert = db.get(Alert, case.alert_id)
    if not alert:
        raise HTTPException(409, "linked alert is unavailable")
    prior = (db.query(AlertReviewDecision)
             .filter(AlertReviewDecision.alert_id == alert.id)
             .order_by(AlertReviewDecision.created_at,
                       AlertReviewDecision.id).all())
    prior_reviewers = {decision.reviewer_id for decision in prior}
    if len(prior_reviewers) < 2:
        raise HTTPException(409, "two independent reviews are required before adjudication")
    if user.id in prior_reviewers:
        raise HTTPException(409, "adjudicator must be independent of both reviewers")

    decided_at = datetime.now(timezone.utc)
    evidence = dict(case.evidence or {})
    evidence.update({
        "adjudication_verdict": body.verdict,
        "adjudicator_user_id": user.id,
        "adjudicated_at": decided_at.isoformat(),
    })
    case.evidence = evidence
    case.reviewed_by = user.id
    case.reviewed_at = decided_at
    case.resolution = body.rationale

    if body.verdict == "unclear":
        case.status = "insufficient_evidence"
        case.training_status = "blocked_insufficient_evidence"
        for image in db.query(TrainingImage).filter(
            TrainingImage.source_alert_id == alert.id,
        ):
            image.eligible_for_training = False
            image.review_state = "quarantined"
        _audit(db, user, "review_disagreement.adjudicated", "assurance_case",
               case.id, {"verdict": body.verdict})
        db.commit()
        return {"case_id": case.id, "status": case.status,
                "verdict": body.verdict, "training_eligible": False}

    final_verdict = "confirmed" if body.verdict == "confirm" else "dismissed"
    db.add(AlertReviewDecision(
        alert_id=alert.id, reviewer_id=user.id, verdict=final_verdict,
        classification="independent_adjudication", note=body.rationale,
    ))
    alert.status = final_verdict
    alert.assigned_to = user.id
    event = db.get(DetectionEvent, alert.event_id)
    quality_control = None
    if event:
        quality_control = (db.query(AlertQualityControl)
                           .filter(AlertQualityControl.camera_id == event.camera_id,
                                   AlertQualityControl.detection_type ==
                                   event.detection_type).one_or_none())
    pair_allows_training = not quality_control or quality_control.mode == "active"
    images = db.query(TrainingImage).filter(
        TrainingImage.source_alert_id == alert.id,
    ).all()
    for image in images:
        image.eligible_for_training = pair_allows_training
        image.review_state = "approved" if pair_allows_training else "quarantined"
        source = dict(image.source_extra or {})
        source.update({
            "adjudication_verdict": final_verdict,
            "adjudicator_user_id": user.id,
            "adjudicated_at": decided_at.isoformat(),
        })
        image.source_extra = source
    alert.training_eligible = pair_allows_training
    case.status = "resolved"
    case.resolved_at = decided_at
    case.training_status = (
        "adjudicated_training_eligible" if pair_allows_training and images
        else "adjudicated_quality_controlled"
    )
    _audit(db, user, "review_disagreement.adjudicated", "assurance_case",
           case.id, {"verdict": body.verdict,
                     "training_eligible": bool(pair_allows_training and images)})
    db.commit()
    if pair_allows_training and images and event:
        from app.training.feedback_loop import _maybe_enqueue_training
        _maybe_enqueue_training(db, event.detection_type)
    return {"case_id": case.id, "status": case.status,
            "verdict": body.verdict,
            "training_eligible": bool(pair_allows_training and images)}


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
async def ingest_odoo_event(
    request: Request,
    db: Session = Depends(get_db),
    timestamp: str = Header(alias="X-VivoGuard-Timestamp"),
    signature: str = Header(alias="X-VivoGuard-Signature"),
):
    raw_body = await request.body()
    _verify_odoo_signature(raw_body, timestamp, signature)
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise TypeError("Odoo webhook body must be a JSON object")
        body = OperationalEventIn(**normalise_odoo_event(payload))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    user = SimpleNamespace(id=None, email="service:odoo-webhook")
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
