"""Per-camera detection configuration API.

  GET   /cameras/{id}/detection-config         — list all
  POST  /cameras/{id}/detection-config         — bulk upsert (one or many)
  DELETE /cameras/{id}/detection-config/{type} — remove a single type
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import Camera, DetectionConfig, DETECTION_TYPES
from app.schemas.detection import DetectionConfigIn, DetectionConfigOut

router = APIRouter(prefix="/cameras", tags=["detection"])


@router.get("/{camera_id}/detection-config", response_model=list[DetectionConfigOut])
def get_detection_config(camera_id: int, db: Session = Depends(get_db),
                         _u=Depends(require_role("admin", "operator", "viewer"))):
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    return db.query(DetectionConfig).filter(DetectionConfig.camera_id == camera_id).all()


@router.post("/{camera_id}/detection-config", response_model=list[DetectionConfigOut])
def upsert_detection_config(camera_id: int, payload: list[DetectionConfigIn],
                            db: Session = Depends(get_db),
                            _u=Depends(require_role("admin", "operator"))):
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    out: list[DetectionConfig] = []
    for item in payload:
        if item.detection_type not in DETECTION_TYPES:
            raise HTTPException(400, f"unknown detection_type: {item.detection_type}")
        row = (db.query(DetectionConfig)
                 .filter(DetectionConfig.camera_id == camera_id,
                         DetectionConfig.detection_type == item.detection_type)
                 .first())
        if not row:
            row = DetectionConfig(camera_id=camera_id, detection_type=item.detection_type)
            db.add(row)
        for k, v in item.model_dump(exclude_unset=True).items():
            setattr(row, k, v)
        out.append(row)
    db.commit()
    for r in out:
        db.refresh(r)
    return out


@router.delete("/{camera_id}/detection-config/{detection_type}")
def delete_detection_config(camera_id: int, detection_type: str,
                            db: Session = Depends(get_db),
                            _u=Depends(require_role("admin", "operator"))):
    row = (db.query(DetectionConfig)
             .filter(DetectionConfig.camera_id == camera_id,
                     DetectionConfig.detection_type == detection_type)
             .first())
    if not row:
        raise HTTPException(404, "config not found")
    db.delete(row)
    db.commit()
    return {"deleted": detection_type}
