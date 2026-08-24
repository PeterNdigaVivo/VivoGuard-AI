"""Stores, shifts, and campaigns API."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


# Stores have unique constraints on `name` and on `code`. Map each
# constraint to a user-friendly message so the UI doesn't show a
# raw "psycopg.errors.UniqueViolation" wall of text.
_UNIQUE_CONFLICT_MESSAGES = {
    "stores_name_key": "A store with this name already exists. Pick a different name.",
    "stores_code_key": "A store with this code already exists. Pick a different code (or leave the code blank).",
}


def _coerce_empty_strings(payload_dict: dict) -> dict:
    """Treat empty-string optional fields as NULL so two blank codes
    don't collide on the unique index."""
    cleaned = {}
    for k, v in payload_dict.items():
        if v == "":
            cleaned[k] = None
        else:
            cleaned[k] = v
    return cleaned


def _explain_db_error(e: Exception) -> tuple[int, str]:
    """Inspect any DB exception and return (http_status, user_message).

    The OLD code relied on `isinstance(e, IntegrityError)` to decide if
    we were looking at a unique-key conflict. That's fragile — psycopg
    errors get re-wrapped by SQLAlchemy in surprising ways, and the
    user kept seeing the misleading "schema is behind the API" message
    for what were really duplicate codes. Now we classify by reading
    the raw text of the error AND its `.orig`. If it mentions a known
    unique constraint or the words `duplicate key` / `UniqueViolation`,
    we surface a clear 409. Only genuinely unknown failures fall
    through to 503.
    """
    raw = str(e)
    if hasattr(e, "orig") and getattr(e, "orig", None) is not None:
        raw = f"{raw}\n{e.orig!s}"

    raw_lower = raw.lower()

    # 1) Known named constraint? Use the friendly message.
    for constraint, friendly in _UNIQUE_CONFLICT_MESSAGES.items():
        if constraint in raw:
            return 409, friendly

    # 2) Generic unique violation we don't have a friendly name for.
    if "uniqueviolation" in raw_lower or "duplicate key" in raw_lower:
        return 409, "That value already exists. Pick a different one."

    # 3) Other integrity issues (FK, NOT NULL, check).
    if isinstance(e, IntegrityError):
        first_line = raw.split("\n")[0][:300]
        return 409, f"Database rejected the row: {first_line}"

    # 4) Anything else — surface the first line. No scary "schema is
    #    behind" speculation; operators can read the api logs for the
    #    full traceback.
    first_line = raw.split("\n")[0][:300]
    return 503, f"Could not save store: {first_line}"


from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import (
    Campaign, Camera, GovernanceAuditLog, OdooStoreMap, Shift, Store,
)
from app.schemas.store import (
    CampaignIn, CampaignOut, ShiftIn, ShiftOut, StoreIn, StoreOut,
)

router = APIRouter(prefix="/stores", tags=["stores"])


# ---------- stores ----------

def _store_deactivation_blockers(db: Session, store_id: int) -> dict[str, int]:
    """Return live integrations that must be moved before retirement.

    Store IDs are durable attribution keys for CCTV evidence, assurance cases
    and Odoo aggregates.  A hard delete can cascade evidence or silently set a
    camera's store to NULL.  Even a soft deactivation is unsafe while live
    camera or POS inputs still target the record, so retirement fails closed
    until those operational links are explicitly reassigned.
    """
    cameras = (db.query(Camera)
                 .filter(Camera.store_id == store_id,
                         Camera.is_deleted.is_(False)).count())
    odoo_maps = (db.query(OdooStoreMap)
                   .filter(OdooStoreMap.store_id == store_id).count())
    return {
        "linked_cameras": cameras,
        "odoo_store_mappings": odoo_maps,
    }


def _deactivate_store(db: Session, store: Store, *, actor=None) -> dict:
    """Soft-deactivate ``store`` without destroying historical attribution."""
    if not store.is_active:
        return {
            "deactivated": store.id,
            "already_inactive": True,
            "historical_data_retained": True,
        }
    blockers = _store_deactivation_blockers(db, store.id)
    if any(blockers.values()):
        raise HTTPException(
            409,
            detail={
                "message": (
                    "Store remains active because operational dependencies "
                    "must be reassigned before deactivation."
                ),
                "store_id": store.id,
                "store_name": store.name,
                "blockers": blockers,
                "required_action": (
                    "Move every linked camera and Odoo store mapping to the "
                    "confirmed destination store, then retry deactivation."
                ),
            },
        )
    store.is_active = False
    db.add(GovernanceAuditLog(
        actor_user_id=getattr(actor, "id", None),
        actor_email=getattr(actor, "email", None),
        action="store.deactivated",
        entity_type="store",
        entity_id=str(store.id),
        details={
            "store_name": store.name,
            "blockers_at_deactivation": blockers,
            "historical_data_retained": True,
        },
    ))
    return {
        "deactivated": store.id,
        "already_inactive": False,
        "historical_data_retained": True,
    }

@router.get("", response_model=list[StoreOut])
def list_stores(include_inactive: bool = False,
                db: Session = Depends(get_db),
                _u=Depends(get_current_user)):
    """List stores. Returns [] (not 500) if the underlying SELECT fails
    because of a schema/migration drift — operators see an empty stores
    page instead of a hard error, and `docker compose logs api` shows
    the precise SQL failure."""
    import logging as _l
    _log = _l.getLogger(__name__)
    try:
        query = db.query(Store)
        if not include_inactive:
            query = query.filter(Store.is_active.is_(True))
        return query.order_by(Store.country, Store.name).all()
    except Exception as e:
        _log.exception("GET /stores failed (likely schema drift): %s", e)
        db.rollback()
        return []


@router.post("", response_model=StoreOut)
def create_store(payload: StoreIn, db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    s = Store(**_coerce_empty_strings(payload.model_dump()))
    db.add(s)
    try:
        db.commit()
    except Exception as e:
        # Single unified handler. _explain_db_error() classifies by
        # message content, so it stays correct no matter which
        # SQLAlchemy class wraps the underlying psycopg error.
        db.rollback()
        import logging as _l
        _l.getLogger(__name__).exception("create_store failed: %s", e)
        status, detail = _explain_db_error(e)
        raise HTTPException(status_code=status, detail=detail) from e
    db.refresh(s)
    return s


@router.get("/{store_id:int}", response_model=StoreOut)
def get_store(store_id: int, db: Session = Depends(get_db),
              _u=Depends(get_current_user)):
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    return s


@router.get("/{store_id:int}/deactivation-impact")
def store_deactivation_impact(store_id: int, db: Session = Depends(get_db),
                              _u=Depends(require_role("admin"))):
    """Preview whether a store can be retired without orphaning live inputs."""
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    blockers = _store_deactivation_blockers(db, store_id)
    return {
        "store_id": store.id,
        "store_name": store.name,
        "is_active": store.is_active,
        "can_deactivate": not any(blockers.values()),
        "blockers": blockers,
        "historical_data_will_be_retained": True,
    }


@router.patch("/{store_id:int}", response_model=StoreOut)
def update_store(store_id: int, patch: StoreIn,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    changes = _coerce_empty_strings(patch.model_dump(exclude_unset=True))
    if changes.get("is_active") is False and s.is_active:
        # Do not let the generic PATCH route bypass the same dependency guard
        # enforced by DELETE.  Retiring a store is an operational workflow,
        # not an ordinary field edit.
        _deactivate_store(db, s, actor=_u)
        changes.pop("is_active", None)
    for k, v in changes.items():
        setattr(s, k, v)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        import logging as _l
        _l.getLogger(__name__).exception("update_store failed: %s", e)
        status, detail = _explain_db_error(e)
        raise HTTPException(status_code=status, detail=detail) from e
    db.refresh(s)
    return s


@router.delete("/{store_id:int}")
def delete_store(store_id: int, db: Session = Depends(get_db),
                 _u=Depends(require_role("admin"))):
    """Retire an unused store while retaining its evidence and audit history.

    This endpoint intentionally preserves the existing HTTP method for client
    compatibility, but no longer issues a destructive ``DELETE`` statement.
    """
    s = db.get(Store, store_id)
    if not s:
        raise HTTPException(404, "store not found")
    result = _deactivate_store(db, s, actor=_u)
    db.commit()
    return result


# ---------- cameras per store ----------

from app.schemas.camera import CameraOut


@router.get("/{store_id:int}/cameras", response_model=list[CameraOut])
def list_store_cameras(store_id: int, db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    return (db.query(Camera)
            .filter(Camera.store_id == store_id, Camera.is_deleted.is_(False))
            .order_by(Camera.id).all())


# ---------- attach / detach cameras ----------

@router.post("/{store_id:int}/cameras/{camera_id}/attach")
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


@router.post("/{store_id:int}/cameras/{camera_id}/detach")
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


@router.post("/{store_id:int}/cameras/bulk-attach", response_model=list[int])
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

@router.get("/{store_id:int}/shifts", response_model=list[ShiftOut])
def list_shifts(store_id: int, db: Session = Depends(get_db),
                _u=Depends(get_current_user)):
    return db.query(Shift).filter(Shift.store_id == store_id).order_by(Shift.day_of_week).all()


@router.post("/{store_id:int}/shifts", response_model=ShiftOut)
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


@router.delete("/{store_id:int}/shifts/{shift_id}")
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


# ---------- zone-health rollup (Feature 4) ----------

@router.get("/zone-health")
def stores_zone_health(db: Session = Depends(get_db),
                       _u=Depends(get_current_user)) -> list[dict]:
    """Per-store zone-health rollup powering the Zone Health dashboard.

    One bulk read (stores → cameras → zones) + per-store pure scoring.
    Returns one row per ACTIVE store with score, badge, per-tag-present
    map, camera counts, and a structured `issues` list (missing tags,
    per-camera duplicates, typo names with suggested rename, offline
    cameras). Empty-camera stores return score=0 / badge=critical /
    "no_cameras" issue rather than crashing.
    """
    from app.models import Camera, Zone
    from app.utils.zone_health import score_store

    stores = (db.query(Store)
                .filter(Store.is_active == True)                          # noqa: E712
                .order_by(Store.name.asc())
                .all())
    if not stores:
        return []
    store_ids = [s.id for s in stores]

    cams = (db.query(Camera)
              .filter(Camera.store_id.in_(store_ids))
              .all())
    cams_by_store: dict[int, list[dict]] = {}
    for c in cams:
        cams_by_store.setdefault(int(c.store_id), []).append({
            "id":     c.id,
            "name":   c.name,
            "status": c.status,
        })

    cam_ids = [c.id for c in cams]
    zones = (db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()
             if cam_ids else [])
    cam_to_store = {int(c.id): int(c.store_id) for c in cams}
    zones_by_store: dict[int, list[dict]] = {}
    for z in zones:
        sid = cam_to_store.get(int(z.camera_id))
        if sid is None:
            continue
        zones_by_store.setdefault(sid, []).append({
            "id":                   z.id,
            "camera_id":            z.camera_id,
            "name":                 z.name,
            "detection_types_json": z.detection_types_json or [],
        })

    out: list[dict] = []
    for s in stores:
        out.append(score_store(
            store_id=int(s.id), store_name=s.name,
            cameras=cams_by_store.get(int(s.id), []),
            zones=zones_by_store.get(int(s.id), []),
        ))
    return out


@router.get("/zone-health.csv")
def stores_zone_health_csv(db: Session = Depends(get_db),
                           _u=Depends(get_current_user)):
    """CSV export of the zone-health rollup. Columns mirror the
    dashboard table so operators can hand a sheet to ops/IT directly.
    Issues collapse to a `;`-separated list."""
    import csv
    import io
    from fastapi.responses import Response

    rows = stores_zone_health(db=db, _u=_u)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "store_id", "store_name", "score", "badge",
        "footfall", "checkout", "staff", "shutter",
        "cameras_total", "cameras_online", "cameras_offline",
        "issue_count", "issues",
    ])
    for r in rows:
        per = r["per_tag_present"]
        w.writerow([
            r["store_id"], r["store_name"], r["score"], r["badge"],
            "yes" if per.get("entry_exit") else "no",
            "yes" if per.get("queue")      else "no",
            "yes" if per.get("staff_zone") else "no",
            "yes" if per.get("shutter")    else "no",
            r["cameras_total"], r["cameras_online"], r["cameras_offline"],
            len(r["issues"]),
            "; ".join(i.get("message", "") for i in r["issues"]),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="zone_health.csv"'},
    )
