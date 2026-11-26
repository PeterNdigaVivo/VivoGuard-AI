"""/model-rollouts — canary, evaluate, manual promote, rollback, fleet view."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import AIModel
from app.models.model_rollout import ModelEvaluation, ModelRollout
from app.services import model_rollout as mr

router = APIRouter(prefix="/model-rollouts", tags=["models"])


class CanaryIn(BaseModel):
    model_id: int
    camera_ids: list[int]
    notes: Optional[str] = None


class PromoteIn(BaseModel):
    # Explicit list; the canary cameras are always included. There is no
    # "all cameras" shortcut on purpose.
    camera_ids: list[int] = []


class EvaluationOut(BaseModel):
    id: int
    model_id: int
    rollout_id: Optional[int] = None
    split: str
    precision: Optional[float] = None
    recall: Optional[float] = None
    false_positive_rate: Optional[float] = None
    sample_count: Optional[int] = None
    passed: Optional[bool] = None
    reasons: list[str] = []
    metrics: Optional[dict] = None
    created_at: Optional[datetime] = None


class RolloutOut(BaseModel):
    id: int
    model_id: int
    model_label: Optional[str] = None
    stage: str
    camera_ids: list[int]
    started_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None
    promoted_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None
    latest_evaluation: Optional[EvaluationOut] = None


def _eval_out(e: ModelEvaluation | None) -> EvaluationOut | None:
    if e is None:
        return None
    return EvaluationOut(
        id=e.id, model_id=e.model_id, rollout_id=e.rollout_id, split=e.split,
        precision=e.precision, recall=e.recall,
        false_positive_rate=e.false_positive_rate, sample_count=e.sample_count,
        passed=e.passed, reasons=list(e.reasons_json or []),
        metrics=e.metrics_json, created_at=e.created_at)


def _out(db: Session, r: ModelRollout) -> RolloutOut:
    m = db.get(AIModel, r.model_id)
    last = (db.query(ModelEvaluation).filter(ModelEvaluation.rollout_id == r.id)
              .order_by(ModelEvaluation.id.desc()).first())
    return RolloutOut(
        id=r.id, model_id=r.model_id,
        model_label=f"{m.name} {m.version}" if m else None, stage=r.stage,
        camera_ids=list(r.camera_ids_json or []), started_at=r.started_at,
        evaluated_at=r.evaluated_at, promoted_at=r.promoted_at,
        rolled_back_at=r.rolled_back_at, latest_evaluation=_eval_out(last))


def _run(db: Session, fn):
    try:
        result = fn()
    except mr.RolloutError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    db.commit()
    return result


@router.get("/fleet")
def fleet_models(db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator", "viewer"))):
    """Base model and, per camera: assigned, effective, canary."""
    return mr.fleet_model_view(db)


@router.get("", response_model=list[RolloutOut])
def list_rollouts(db: Session = Depends(get_db),
                  _u=Depends(require_role("admin", "operator", "viewer"))):
    rows = db.query(ModelRollout).order_by(ModelRollout.id.desc()).limit(100).all()
    return [_out(db, r) for r in rows]


@router.get("/models/{model_id}/evaluations", response_model=list[EvaluationOut])
def model_evaluations(model_id: int, db: Session = Depends(get_db),
                      _u=Depends(require_role("admin", "operator", "viewer"))):
    rows = (db.query(ModelEvaluation).filter(ModelEvaluation.model_id == model_id)
              .order_by(ModelEvaluation.id.desc()).all())
    return [_eval_out(e) for e in rows]


@router.post("/canary", response_model=RolloutOut)
def start_canary(body: CanaryIn, db: Session = Depends(get_db),
                 user=Depends(require_role("admin"))):
    r = _run(db, lambda: mr.start_canary(db, body.model_id, body.camera_ids,
                                         user_id=user.id, notes=body.notes))
    return _out(db, r)


@router.post("/{rollout_id}/evaluate", response_model=EvaluationOut)
def evaluate(rollout_id: int, db: Session = Depends(get_db),
             _u=Depends(require_role("admin", "operator"))):
    r = db.get(ModelRollout, rollout_id)
    if r is None:
        raise HTTPException(404, "rollout not found")
    return _eval_out(_run(db, lambda: mr.evaluate(db, r)))


@router.post("/{rollout_id}/promote", response_model=RolloutOut)
def promote(rollout_id: int, body: PromoteIn, db: Session = Depends(get_db),
            user=Depends(require_role("admin"))):
    r = db.get(ModelRollout, rollout_id)
    if r is None:
        raise HTTPException(404, "rollout not found")
    return _out(db, _run(db, lambda: mr.promote(db, r, body.camera_ids,
                                                user_id=user.id)))


@router.post("/{rollout_id}/rollback", response_model=RolloutOut)
def rollback(rollout_id: int, db: Session = Depends(get_db),
             user=Depends(require_role("admin"))):
    r = db.get(ModelRollout, rollout_id)
    if r is None:
        raise HTTPException(404, "rollout not found")
    return _out(db, _run(db, lambda: mr.rollback(db, r, user_id=user.id)))
