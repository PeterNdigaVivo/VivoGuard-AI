"""Stores, shifts, and campaigns API."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import Campaign, Camera, Shift, Store
from app.schemas.store import (
    CampaignIn, CampaignOut, ShiftIn, ShiftOut, StoreIn, StoreOut,
)

router = APIRouter(prefix="/stores", tags=["stores"])


# ---------- stores ----------

@router.get("", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """List stores. Returns [] (not 500) if the underlying SELECT fails
    because of a schema/migration drift — operators see an empty stores
    page instead of a hard error, and `docker compose logs api` shows
    the precise SQL failure."""
    import logging as _l
    _log = _l.getLogger(__name__)
    try:
        return db.query(Store).order_by(Store.country, Store.name).all()
    except Exception as e:
        # Most common cause: alembic_version table at head but
        # manager_name / manager_phone columns missing on `stores`.
        # Fix: run  docker compose exec api alembic stamp 0003
        #       then docker compose exec api alembic upgrade head
        _log.exception("GET /stores failed (likely schema drift): %s", e)
        db.rollback()
        return []


@router.post("", response_model=StoreOut)
def create_store(payload: StoreIn, db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    s = Store(**payload.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return s


@router.get("/{store_id}", response_model=StoreOut)
def get_store(store_id: int, db: Session = Depends(get_db),
              _u=Depends(get_current_user)):
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    return s


@router.patch("/{store_id}", response_model=StoreOut)
def update_store(store_id: int, patch: StoreIn,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    for k, v in patch.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit(); db.refresh(s)
    return s


@router.delete("/{store_id}")
def delete_store(store_id: int, db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    db.delete(s); db.commit()
    return {"deleted": store_id}


# ---------- cameras per store ----------

from app.schemas.camera import CameraOut


@router.get("/{store_id}/cameras", response_model=list[CameraOut])
def list_store_cameras(store_id: int, db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    return db.query(Camera).filter(Camera.store_id == store_id).order_by(Camera.id).all()


# ---------- attach / detach cameras ----------

@router.post("/{store_id}/cameras/{camera_id}/attach")
def attach_camera(store_id: int, camera_id: int,
                  db: Session = Depends(get_db),
                  _u=Depends(require_role("admin", "operator"))):
    store = db.get(Store, store_id)
    cam = db.get(Camera, camera_id)
    if not store or not cam:
        raise HTTPException(404, "store or camera not found")
    cam.store_id = store_id
    db.commit()
    return {"camera_id": cam.id, "store_id": store_id}


@router.post("/{store_id}/cameras/{camera_id}/detach")
def detach_camera(store_id: int, camera_id: int,
                  db: Session = Depends(get_db),
                  _u=Depends(require_role("admin", "operator"))):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    if cam.store_id != store_id:
        raise HTTPException(409, "camera is not attached to this store")
    cam.store_id = None
    db.commit()
    return {"camera_id": cam.id, "store_id": None}


# Bulk attach — operator picks N cameras from the chain and attaches
# them to one store in a single call. Returns the list of affected IDs.
from pydantic import BaseModel
class BulkAttach(BaseModel):
    camera_ids: list[int]


@router.post("/{store_id}/cameras/bulk-attach", response_model=list[int])
def bulk_attach(store_id: int, payload: BulkAttach,
                db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator"))):
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    affected: list[int] = []
    for cid in payload.camera_ids:
        cam = db.get(Camera, cid)
        if cam:
            cam.store_id = store_id
            affected.append(cam.id)
    db.commit()
    return affected


# ---------- shifts ----------

@router.get("/{store_id}/shifts", response_model=list[ShiftOut])
def list_shifts(store_id: int, db: Session = Depends(get_db),
                _u=Depends(get_current_user)):
    return db.query(Shift).filter(Shift.store_id == store_id).order_by(Shift.day_of_week).all()


@router.post("/{store_id}/shifts", response_model=ShiftOut)
def create_shift(store_id: int, payload: ShiftIn,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator"))):
    if payload.store_id != store_id:
        raise HTTPException(400, "store_id mismatch")
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    s = Shift(**payload.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return s


@router.delete("/{store_id}/shifts/{shift_id}")
def delete_shift(store_id: int, shift_id: int,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator"))):
    s = db.get(Shift, shift_id)
    if not s or s.store_id != store_id:
        raise HTTPException(404, "shift not found")
    db.delete(s); db.commit()
    return {"deleted": shift_id}


# ---------- campaigns ----------

@router.get("/campaigns/all", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db),
                   _u=Depends(get_current_user)):
    return db.query(Campaign).order_by(Campaign.start_date.desc()).all()


@router.post("/campaigns", response_model=CampaignOut)
def create_campaign(payload: CampaignIn, db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator"))):
    c = Campaign(**payload.model_dump())
    db.add(c); db.commit(); db.refresh(c)
    return c
