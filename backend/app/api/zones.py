"""Zone CRUD API.

  GET    /cameras/{id}/zones
  POST   /cameras/{id}/zones          — create or replace one zone (by name)
  DELETE /cameras/{id}/zones/{zone_id}
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import Camera, Zone
from app.schemas.detection import ZoneIn, ZoneOut

router = APIRouter(prefix="/cameras", tags=["zones"])


@router.get("/{camera_id}/zones", response_model=list[ZoneOut])
def list_zones(camera_id: int, db: Session = Depends(get_db),
               _u=Depends(require_role("admin", "operator", "viewer"))):
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    return db.query(Zone).filter(Zone.camera_id == camera_id).all()


@router.post("/{camera_id}/zones", response_model=ZoneOut)
def create_zone(camera_id: int, payload: ZoneIn, db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator"))):
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    z = Zone(camera_id=camera_id, **payload.model_dump())
    db.add(z)
    db.commit()
    db.refresh(z)
    return z


@router.put("/{camera_id}/zones/{zone_id}", response_model=ZoneOut)
def update_zone(camera_id: int, zone_id: int, payload: ZoneIn,
                db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator"))):
    z = db.get(Zone, zone_id)
    if not z or z.camera_id != camera_id:
        raise HTTPException(404, "zone not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(z, k, v)
    db.commit()
    db.refresh(z)
    return z


@router.delete("/{camera_id}/zones/{zone_id}")
def delete_zone(camera_id: int, zone_id: int, db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator"))):
    z = db.get(Zone, zone_id)
    if not z or z.camera_id != camera_id:
        raise HTTPException(404, "zone not found")
    db.delete(z)
    db.commit()
    return {"deleted": zone_id}
