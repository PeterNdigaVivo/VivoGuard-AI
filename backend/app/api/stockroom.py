"""Stockroom access audit-trail API (P3).

  GET /stockroom/accesses               — list (filterable)
  GET /stockroom/accesses.csv           — CSV export for compliance
  POST /stockroom/accesses/{id}/mark-authorised
"""
from __future__ import annotations
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import StockroomAccess

router = APIRouter(prefix="/stockroom", tags=["stockroom"])


def _q(db: Session, **filters):
    q = db.query(StockroomAccess)
    if filters.get("store_id"):  q = q.filter(StockroomAccess.store_id  == filters["store_id"])
    if filters.get("camera_id"): q = q.filter(StockroomAccess.camera_id == filters["camera_id"])
    if filters.get("direction"): q = q.filter(StockroomAccess.direction == filters["direction"])
    if filters.get("since"):     q = q.filter(StockroomAccess.timestamp >= filters["since"])
    if filters.get("until"):     q = q.filter(StockroomAccess.timestamp <= filters["until"])
    return q.order_by(desc(StockroomAccess.timestamp))


@router.get("/accesses")
def list_accesses(
    db: Session = Depends(get_db), _u=Depends(get_current_user),
    store_id: Optional[int] = None,
    camera_id: Optional[int] = None,
    direction: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(500, le=5000),
):
    rows = _q(db, store_id=store_id, camera_id=camera_id, direction=direction,
              since=since, until=until).limit(limit).all()
    return [
        {
            "id": r.id, "store_id": r.store_id, "camera_id": r.camera_id,
            "zone_id": r.zone_id, "direction": r.direction,
            "track_id": r.track_id, "is_authorised": r.is_authorised,
            "snapshot_path": r.snapshot_path,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        } for r in rows
    ]


@router.get("/accesses.csv")
def export_csv(
    db: Session = Depends(get_db), _u=Depends(get_current_user),
    store_id: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
):
    rows = _q(db, store_id=store_id, since=since, until=until).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "store_id", "camera_id", "zone_id", "direction",
                "track_id", "is_authorised", "snapshot_path", "timestamp"])
    for r in rows:
        w.writerow([r.id, r.store_id, r.camera_id, r.zone_id, r.direction,
                    r.track_id, r.is_authorised, r.snapshot_path or "",
                    r.timestamp.isoformat() if r.timestamp else ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stockroom_audit.csv"'},
    )


@router.post("/accesses/{access_id}/mark-authorised")
def mark_authorised(access_id: int, is_authorised: bool = True,
                    db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator"))):
    row = db.get(StockroomAccess, access_id)
    if not row:
        raise HTTPException(404, "row not found")
    row.is_authorised = is_authorised
    db.commit()
    return {"id": row.id, "is_authorised": row.is_authorised}
