"""Admin-only API for the evaluation test set (Evaluation page).

System admins only (same guard as System Health): the test set holds
images of people. Serves test images/clips from settings.eval_dir and
records the admins' labels — a click on each visible person (people
frames) or a people count (clips).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.system_health import require_system_admin
from app.config import settings
from app.database import get_db
from app.models import EvalSample, User

router = APIRouter(prefix="/eval", tags=["evaluation"])

LabelKind = Literal["people", "clip"]
MAX_POINTS = 200


class SampleOut(BaseModel):
    id: int
    kind: str
    camera_id: int | None
    captured_at: datetime | None
    media_type: str


class LabelIn(BaseModel):
    points: list[list[float]] | None = Field(default=None, max_length=MAX_POINTS)
    count: int | None = Field(default=None, ge=0, le=MAX_POINTS)
    skip: bool = False

    @field_validator("points")
    @classmethod
    def _points_in_frame(cls, v):
        if v is None:
            return v
        for p in v:
            if len(p) != 2 or not all(0.0 <= c <= 1.0 for c in p):
                raise ValueError("each point must be [x, y] with 0 <= x, y <= 1")
        return v


def _sample_out(s: EvalSample) -> SampleOut:
    return SampleOut(id=s.id, kind=s.kind, camera_id=s.camera_id,
                     captured_at=s.captured_at,
                     media_type="video/mp4" if s.kind == "clip" else "image/jpeg")


@router.get("/summary")
def summary(db: Session = Depends(get_db), _u: User = Depends(require_system_admin)) -> dict:
    alerts: dict[str, dict] = {}
    for category, verdict, n in (db.query(EvalSample.category, EvalSample.verdict,
                                          func.count(EvalSample.id))
                                   .filter(EvalSample.kind == "alert")
                                   .group_by(EvalSample.category, EvalSample.verdict)):
        row = alerts.setdefault(category or "other", {"total": 0, "true": 0, "false": 0})
        row["total"] += n
        if verdict in ("true", "false"):
            row[verdict] += n
    labels: dict[str, dict] = {"people": {}, "clip": {}}
    for kind, status, n in (db.query(EvalSample.kind, EvalSample.status, func.count(EvalSample.id))
                              .filter(EvalSample.kind.in_(("people", "clip")))
                              .group_by(EvalSample.kind, EvalSample.status)):
        labels[kind][status] = n
    return {
        "collection_enabled": bool(settings.eval_capture_raw_frames),
        "targets": {"per_alert_type": settings.eval_target_per_type,
                    "people": settings.eval_people_target, "clips": settings.eval_clip_target},
        "retention_days": settings.eval_retention_days,
        "alerts": alerts,
        "people": labels["people"],
        "clips": labels["clip"],
    }


@router.get("/next", response_model=SampleOut | None)
def next_sample(kind: LabelKind, db: Session = Depends(get_db),
                _u: User = Depends(require_system_admin)):
    s = (db.query(EvalSample)
           .filter(EvalSample.kind == kind, EvalSample.status == "pending")
           .order_by(EvalSample.created_at, EvalSample.id).first())
    return _sample_out(s) if s else None


def _safe_path(raw: str | None) -> Path:
    """The file must exist and live inside the eval directory."""
    if not raw:
        raise HTTPException(404, "no file for this sample")
    root = Path(settings.eval_dir).resolve()
    path = Path(raw).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "file not available")
    return path


@router.get("/samples/{sample_id}/media")
def media(sample_id: int, db: Session = Depends(get_db),
          _u: User = Depends(require_system_admin)):
    s = db.get(EvalSample, sample_id)
    if s is None:
        raise HTTPException(404, "sample not found")
    path = _safe_path(s.clip_path if s.kind == "clip" else s.frame_path)
    return FileResponse(path, media_type="video/mp4" if s.kind == "clip" else "image/jpeg",
                        headers={"Cache-Control": "no-store"})


@router.post("/samples/{sample_id}/label")
def label(sample_id: int, body: LabelIn, db: Session = Depends(get_db),
          user: User = Depends(require_system_admin)) -> dict:
    s = db.get(EvalSample, sample_id)
    if s is None or s.kind not in ("people", "clip"):
        raise HTTPException(404, "labelling sample not found")
    if body.skip:
        s.status, s.label = "skipped", None
    elif s.kind == "people":
        if body.points is None:
            raise HTTPException(422, "people frames need `points` (one click per person)")
        s.status, s.label = "labelled", {"points": body.points}
    else:
        if body.count is None:
            raise HTTPException(422, "clips need `count` (people in the clip)")
        s.status, s.label = "labelled", {"count": body.count}
    s.labelled_by = user.id
    s.labelled_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": s.id, "status": s.status}
