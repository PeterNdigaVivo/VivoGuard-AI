"""System-admin read view for Odoo mapping and advisory health."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.system_health import require_system_admin
from app.config import settings
from app.database import get_db
from app.models import (
    AssuranceCase, OdooConversionMetric, OdooStoreMap, OdooSyncState,
    OdooTillConflict, Store,
)

router = APIRouter(prefix="/odoo", tags=["odoo-read-only"])


@router.get("/assurance")
def assurance_dashboard(db: Session = Depends(get_db),
                        _user=Depends(require_system_admin)) -> dict:
    stores = db.query(Store).filter(Store.is_active.is_(True)).order_by(Store.name).all()
    maps = {row.store_id: row for row in db.query(OdooStoreMap).all()}
    mappings = []
    for store in stores:
        mapping = maps.get(store.id)
        mappings.append({
            "store_id": store.id,
            "store_name": store.name,
            "mapped": mapping is not None,
            "odoo_model": mapping.odoo_model if mapping else None,
            "odoo_res_id": mapping.odoo_res_id if mapping else None,
            "odoo_pos_config_id": mapping.odoo_pos_config_id if mapping else None,
            "odoo_name": mapping.name if mapping else None,
            "last_synced_at": mapping.last_synced_at if mapping else None,
            "sync_error": mapping.sync_error if mapping else None,
        })
    sync = db.query(OdooSyncState).order_by(OdooSyncState.stream).all()
    conflicts = (db.query(OdooTillConflict).filter(OdooTillConflict.status == "open")
                 .order_by(OdooTillConflict.business_day.desc()).limit(100).all())
    conversion = (db.query(OdooConversionMetric)
                  .filter(OdooConversionMetric.period_start >=
                          datetime.now(timezone.utc) - timedelta(days=2))
                  .order_by(OdooConversionMetric.period_start.desc()).limit(200).all())
    changing = (db.query(AssuranceCase)
                .filter(AssuranceCase.case_type == "changing_room_review",
                        AssuranceCase.status.in_(("open", "investigating", "pending_human_review")))
                .order_by(AssuranceCase.last_seen_at.desc()).limit(100).all())
    return {
        "enabled": settings.odoo_sync_enabled,
        "mode": "read_only",
        "mapped": sum(1 for row in mappings if row["mapped"]),
        "unmapped": sum(1 for row in mappings if not row["mapped"]),
        "mappings": mappings,
        "sync": [{
            "stream": row.stream, "last_attempt_at": row.last_attempt_at,
            "last_success_at": row.last_success_at,
            "consecutive_failures": row.consecutive_failures,
            "circuit_open_until": row.circuit_open_until,
            "last_error": row.last_error,
        } for row in sync],
        "till_conflicts": [{
            "id": row.id, "store_id": row.store_id,
            "business_day": row.business_day, "conflict_type": row.conflict_type,
            "camera_event_at": row.camera_event_at, "till_event_at": row.till_event_at,
            "status": row.status,
        } for row in conflicts],
        "conversion": [{
            "store_id": row.store_id, "period_start": row.period_start,
            "footfall": row.footfall, "transactions": row.transactions,
            "conversion_rate": row.conversion_rate,
            "data_quality_flag": row.data_quality_flag,
        } for row in conversion],
        "changing_room_reviews": [{
            "id": row.id, "store_id": row.store_id, "camera_id": row.camera_id,
            "title": row.title, "status": row.status, "evidence": row.evidence,
        } for row in changing],
        "guarantees": {
            "writes_to_odoo": False,
            "alerts_suppressed_by_odoo": False,
            "human_review_required": True,
        },
    }
