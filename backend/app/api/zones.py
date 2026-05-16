"""Zone CRUD API.

  GET    /cameras/{id}/zones
  POST   /cameras/{id}/zones          — create or replace one zone (by name)
  DELETE /cameras/{id}/zones/{zone_id}
  GET    /zone-purposes               — plain-English purpose catalog
  POST   /cameras/{id}/zones/by-purpose — create zone with a purpose key

Side effect on create/update: every detection_type listed in the zone's
detection_types_json is auto-enabled in detection_configs (creating the
row if it doesn't exist). The inference worker also does this lookup
in-memory so even pre-existing zones light up after this commit, but
persisting it keeps the AI Settings page truthful.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import Camera, DetectionConfig, Zone
from app.schemas.detection import ZoneIn, ZoneOut
from app.zone_purposes import ZONE_PURPOSES, types_for_purpose

router = APIRouter(prefix="/cameras", tags=["zones"])


# Catalog — frontend renders the dropdown from this. Mounted under the
# zones router prefix so it's discoverable next to the CRUD endpoints,
# but with a different path so no conflict.
catalog_router = APIRouter(tags=["zones"])

@catalog_router.get("/zone-purposes")
def zone_purposes():
    return ZONE_PURPOSES


class ZoneByPurposeIn(BaseModel):
    name: str
    purpose: str                          # key into ZONE_PURPOSES
    polygon_coords_json: list[list[float]]
    active_schedule_json: dict | None = None


@catalog_router.post("/cameras/{camera_id}/zones/by-purpose", response_model=ZoneOut)
def create_zone_by_purpose(camera_id: int, payload: ZoneByPurposeIn,
                           db: Session = Depends(get_db),
                           _u=Depends(require_role("admin", "operator"))):
    purpose = ZONE_PURPOSES.get(payload.purpose)
    if not purpose:
        raise HTTPException(400, f"unknown purpose: {payload.purpose}")
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    z = Zone(
        camera_id=camera_id,
        name=payload.name,
        shape=purpose["shape"],
        polygon_coords_json=payload.polygon_coords_json,
        detection_types_json=list(purpose["types"]),
        active_schedule_json=payload.active_schedule_json,
        suppressed=False,
    )
    db.add(z)
    _autoenable_detection_types(db, camera_id, purpose["types"])
    db.commit(); db.refresh(z)
    return z


def _autoenable_detection_types(db: Session, camera_id: int, types: list[str]) -> None:
    """For every type in `types`, ensure a DetectionConfig row exists with
    enabled=True. Idempotent. Doesn't touch confidence/dwell knobs on
    pre-existing rows so operators can still tune them per type."""
    for t in types or []:
        row = (db.query(DetectionConfig)
                 .filter(DetectionConfig.camera_id == camera_id,
                         DetectionConfig.detection_type == t)
                 .first())
        if row is None:
            db.add(DetectionConfig(
                camera_id=camera_id, detection_type=t,
                enabled=True, confidence_threshold=0.4,
                min_object_size=0, detection_every_n_frames=1,
            ))
        elif not row.enabled:
            row.enabled = True


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
    _autoenable_detection_types(db, camera_id, payload.detection_types_json)
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
    _autoenable_detection_types(db, camera_id, payload.detection_types_json or [])
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
