"""/training-evidence — the review queue between operator verdicts and models."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import Annotation, TrainingImage
from app.services import training_evidence as te

router = APIRouter(prefix="/training-evidence", tags=["training"])


class EvidenceOut(BaseModel):
    id: int
    promotion_path: str | None = None
    audit_sample: bool = False
    labelled_by_user_id: int | None = None
    labelled_at: str | None = None
    gate: dict | None = None
    dataset_id: int | None = None
    camera_id: int | None = None
    review_state: str
    example_kind: str | None = None
    target_class: str | None = None
    source_kind: str
    source_alert_id: int | None = None
    detection_type: str | None = None
    dismiss_reason: str | None = None
    box_count: int = 0
    preview_url: str | None = None


class EvidencePage(BaseModel):
    total: int
    items: list[EvidenceOut]
    counts: dict[str, int]


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject", "exclude"]
    kind: Optional[Literal["positive", "negative", "uncertain", "policy_error"]] = None
    target_class: Optional[str] = None


# Training Review is an EXCEPTIONS queue: operator marks train directly
# (app.services.label_promotion); only these buckets need a person.
BUCKETS = ("disagreement", "burst", "audit", "ambiguous", "approved",
           "rejected", "excluded")


def _bucket_filter(q, bucket: str):
    pending = TrainingImage.review_state.in_(te.PENDING_STATES)
    if bucket == "disagreement":
        return q.filter(pending, TrainingImage.promotion_path == "held_disagreement")
    if bucket == "burst":
        return q.filter(pending, TrainingImage.promotion_path == "held_burst")
    if bucket == "audit":
        return q.filter(TrainingImage.audit_sample.is_(True),
                        TrainingImage.promotion_path.in_(("auto_agreement", "auto_trusted")))
    if bucket == "ambiguous":
        # Everything else still waiting: person False without a choice,
        # labels made while direct promotion was off, legacy backlog.
        return q.filter(pending, ~TrainingImage.promotion_path.in_(
            ("held_disagreement", "held_burst")) | TrainingImage.promotion_path.is_(None))
    return q.filter(TrainingImage.review_state == {
        "approved": te.APPROVED, "rejected": te.REJECTED, "excluded": te.EXCLUDED}[bucket])


def _out(r: TrainingImage, boxes: int) -> EvidenceOut:
    ex = r.source_extra or {}
    return EvidenceOut(
        id=r.id, dataset_id=r.dataset_id, camera_id=r.camera_id,
        review_state=r.review_state, example_kind=r.example_kind,
        target_class=r.target_class, source_kind=r.source_kind,
        source_alert_id=r.source_alert_id,
        detection_type=ex.get("detection_type"),
        dismiss_reason=ex.get("dismiss_reason"),
        box_count=int(boxes),
        preview_url=(f"/api/alerts/{r.source_alert_id}/snapshot"
                     if r.source_alert_id else None),
        promotion_path=r.promotion_path, audit_sample=bool(r.audit_sample),
        labelled_by_user_id=r.labelled_by_user_id,
        labelled_at=r.labelled_at.isoformat() if r.labelled_at else None,
        gate=r.gate_json)


@router.get("", response_model=EvidencePage)
def list_evidence(
    bucket: Literal["disagreement", "burst", "audit", "ambiguous", "approved",
                    "rejected", "excluded"] = "disagreement",
    kind: Optional[str] = Query(None),
    target_class: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    base = db.query(TrainingImage).filter(TrainingImage.example_kind.isnot(None))
    if kind:
        base = base.filter(TrainingImage.example_kind == kind)
    if target_class:
        base = base.filter(TrainingImage.target_class == target_class)
    q = _bucket_filter(base, bucket)
    total = q.count()
    rows = q.order_by(TrainingImage.id.desc()).offset(offset).limit(limit).all()
    ids = [r.id for r in rows]
    boxes = dict(db.query(Annotation.image_id, func.count(Annotation.id))
                   .filter(Annotation.image_id.in_(ids))
                   .group_by(Annotation.image_id).all()) if ids else {}
    counts = {b: _bucket_filter(base, b).count() for b in BUCKETS}
    return EvidencePage(total=total, items=[_out(r, boxes.get(r.id, 0)) for r in rows],
                        counts=counts)


@router.post("/{image_id}/review", response_model=EvidenceOut)
def review_evidence(image_id: int, body: ReviewIn,
                    db: Session = Depends(get_db),
                    user=Depends(require_role("admin", "operator"))):
    img = db.get(TrainingImage, image_id)
    if img is None:
        raise HTTPException(404, "evidence not found")
    try:
        te.review(db, img, body.decision, reviewer_id=user.id,
                  kind=body.kind, target_class=body.target_class)
    except te.ReviewError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    from app.models import GovernanceAuditLog
    db.add(GovernanceAuditLog(
        actor_user_id=user.id, actor_email=getattr(user, "email", None),
        action=f"training_review.{body.decision}", entity_type="training_image",
        entity_id=str(img.id),
        details={"kind": img.example_kind, "target_class": img.target_class,
                 "promotion_path": img.promotion_path,
                 "alert_id": img.source_alert_id}))
    db.commit()
    n = db.query(func.count(Annotation.id)).filter(
        Annotation.image_id == img.id).scalar() or 0
    return _out(img, n)


@router.get("/readiness")
def training_readiness(
    target_class: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator", "viewer")),
):
    """Per-class readiness: approved positives/negatives vs requirements."""
    if target_class:
        classes = [target_class]
    else:
        classes = sorted({c for (c,) in
                          db.query(TrainingImage.target_class).distinct()
                          if c})
    return [te.readiness(db, c) for c in classes]
