"""/alerts endpoints — list with filters, confirm, dismiss."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import Alert, Camera, DetectionEvent, User
from app.schemas.alert import AlertActionOut, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
    camera_id: Optional[int]   = Query(None),
    detection_type: Optional[str] = Query(None),
    zone_id: Optional[int]     = Query(None),
    status: Optional[str]      = Query(None),
    since: Optional[datetime]  = Query(None),
    until: Optional[datetime]  = Query(None),
    limit: int                 = Query(100, le=500),
):
    q = (db.query(Alert, DetectionEvent, Camera)
           .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
           .outerjoin(Camera, DetectionEvent.camera_id == Camera.id))
    if status:
        q = q.filter(Alert.status == status)
    if camera_id:
        q = q.filter(DetectionEvent.camera_id == camera_id)
    if detection_type:
        q = q.filter(DetectionEvent.detection_type == detection_type)
    if zone_id:
        q = q.filter(DetectionEvent.zone_id == zone_id)
    if since:
        q = q.filter(DetectionEvent.timestamp >= since)
    if until:
        q = q.filter(DetectionEvent.timestamp <= until)
    q = q.order_by(desc(Alert.created_at)).limit(limit)

    out: list[AlertOut] = []
    for alert, event, camera in q.all():
        item = AlertOut.model_validate(alert)
        item.camera_id      = event.camera_id
        item.camera_name    = camera.name if camera else None
        item.detection_type = event.detection_type
        item.confidence     = event.confidence
        item.bbox_norm      = event.bbox_json
        item.zone_id        = event.zone_id
        item.thumbnail_path = event.thumbnail_path
        out.append(item)
    return out


@router.post("/{alert_id}/confirm", response_model=AlertActionOut)
def confirm(alert_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_role("admin", "operator"))):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = "confirmed"
    a.assigned_to = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    return AlertActionOut(id=a.id, status=a.status)


@router.post("/{alert_id}/dismiss", response_model=AlertActionOut)
def dismiss(alert_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_role("admin", "operator"))):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = "dismissed"
    a.assigned_to = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    return AlertActionOut(id=a.id, status=a.status)
