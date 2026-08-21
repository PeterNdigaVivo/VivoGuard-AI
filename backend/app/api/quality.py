"""Human-governed alert quality controls and evidence scorecards."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import AlertQualityControl, Camera, User
from app.services.alert_quality import quality_scorecards, set_manual_mode

router = APIRouter(prefix="/quality", tags=["quality"])


class QualityModeIn(BaseModel):
    mode: Literal["active", "review_only", "quarantined"]
    reason: str = Field(min_length=5, max_length=512)
    force: bool = False


def _control_out(row: AlertQualityControl) -> dict:
    return {
        "camera_id": row.camera_id, "detection_type": row.detection_type,
        "mode": row.mode, "source": row.source, "reason": row.reason,
        "changed_by": row.changed_by,
        "changed_at": row.changed_at.isoformat() if row.changed_at else None,
        "quarantined_at": (row.quarantined_at.isoformat()
                           if row.quarantined_at else None),
        "rolling_sample_size": row.last_sample_size,
        "rolling_false_rate": row.last_false_rate,
    }


@router.get("/scorecards")
def scorecards(days: int = Query(7, ge=1, le=90),
               db: Session = Depends(get_db),
               _user: User = Depends(get_current_user)):
    return {
        "window_days": days,
        "scorecards": quality_scorecards(db, days=days),
        "warning": "Precision excludes unreviewed alerts; recall requires independently reported missed events.",
    }


@router.get("/controls")
def controls(db: Session = Depends(get_db),
             _user: User = Depends(get_current_user)):
    rows = (db.query(AlertQualityControl)
              .order_by(AlertQualityControl.camera_id,
                        AlertQualityControl.detection_type).all())
    return {"controls": [_control_out(row) for row in rows]}


@router.put("/controls/{camera_id}/{detection_type}")
def change_control(camera_id: int, detection_type: str, body: QualityModeIn,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_role("admin"))):
    if db.get(Camera, camera_id) is None:
        raise HTTPException(404, "camera not found")
    # Force-release is intentionally admin-only (as is this entire route),
    # reasoned and attributable. Normal release remains evidence-gated.
    try:
        row = set_manual_mode(db, camera_id, detection_type, body.mode,
                              reason=body.reason, actor=user.email,
                              force=body.force)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _control_out(row)
