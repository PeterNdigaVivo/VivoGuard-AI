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


@router.get("", response_model=EvidencePage)
def list_evidence(
    state: Literal["pending_review", "approved", "rejected", "excluded"] = "pending_review",
    kind: Optional[str] = Query(None),
    target_class: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    base = db.query(TrainingImage).filter(TrainingImage.example_kind.isnot(None))
    states = te.PENDING_STATES if state == te.PENDING else (state,)
    q = base.filter(TrainingImage.review_state.in_(states))
    if kind:
        q = q.filter(TrainingImage.example_kind == kind)
    if target_class:
        q = q.filter(TrainingImage.target_class == target_class)
    total = q.count()
    rows = q.order_by(TrainingImage.id.desc()).offset(offset).limit(limit).all()
    ids = [r.id for r in rows]
    boxes = dict(db.query(Annotation.image_id, func.count(Annotation.id))
                   .filter(Annotation.image_id.in_(ids))
                   .group_by(Annotation.image_id).all()) if ids else {}
    counts = {k: int(n) for k, n in
              base.filter(TrainingImage.review_state.in_(states))
                  .with_entities(TrainingImage.example_kind, func.count(TrainingImage.id))
                  .group_by(TrainingImage.example_kind).all()}
    items = []
    for r in rows:
        ex = r.source_extra or {}
        items.append(EvidenceOut(
            id=r.id, dataset_id=r.dataset_id, camera_id=r.camera_id,
            review_state=r.review_state, example_kind=r.example_kind,
            target_class=r.target_class, source_kind=r.source_kind,
            source_alert_id=r.source_alert_id,
            detection_type=ex.get("detection_type"),
            dismiss_reason=ex.get("dismiss_reason"),
            box_count=int(boxes.get(r.id, 0)),
            preview_url=(f"/api/alerts/{r.source_alert_id}/snapshot"
                         if r.source_alert_id else None),
        ))
    return EvidencePage(total=total, items=items, counts=counts)


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
    db.commit()
    ex = img.source_extra or {}
    n = db.query(func.count(Annotation.id)).filter(
        Annotation.image_id == img.id).scalar() or 0
    return EvidenceOut(
        id=img.id, dataset_id=img.dataset_id, camera_id=img.camera_id,
        review_state=img.review_state, example_kind=img.example_kind,
        target_class=img.target_class, source_kind=img.source_kind,
        source_alert_id=img.source_alert_id,
        detection_type=ex.get("detection_type"),
        dismiss_reason=ex.get("dismiss_reason"), box_count=int(n))


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
