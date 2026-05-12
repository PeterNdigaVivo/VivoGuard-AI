"""Retail analytics read API.

  GET /analytics/metrics
        ?metric_type=queue_length
        &store_id=...
        &camera_id=...
        &since=...&until=...

  GET /analytics/dashboard/store/{store_id}
        — single-store rollup of key KPIs
  GET /analytics/dashboard/multi
        — cross-store comparison
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.analytics import recorder
from app.database import get_db
from app.deps import get_current_user
from app.models import (
    Alert, Camera, DetectionEvent, MetricSnapshot, Store, VisitorTrack,
)
from app.schemas.store import MetricSnapshotOut

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/metrics", response_model=list[MetricSnapshotOut])
def metrics(
    metric_type: str,
    db: Session = Depends(get_db), _u=Depends(get_current_user),
    store_id: Optional[int] = None,
    camera_id: Optional[int] = None,
    zone_id: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(1440, le=10000),
):
    return recorder.query(db, metric_type,
                          store_id=store_id, camera_id=camera_id, zone_id=zone_id,
                          since=since, until=until, limit=limit)


@router.get("/dashboard/store/{store_id}")
def store_dashboard(store_id: int, days: int = 7,
                    db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """Roll up the most-used KPIs for a single store for the last N days."""
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    since = datetime.now(timezone.utc) - timedelta(days=days)

    def _last(metric: str) -> float | None:
        row = (db.query(MetricSnapshot)
                 .filter(MetricSnapshot.store_id == store_id,
                         MetricSnapshot.metric_type == metric)
                 .order_by(MetricSnapshot.period_start.desc())
                 .first())
        return row.value if row else None

    def _avg(metric: str) -> float | None:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(MetricSnapshot.store_id == store_id,
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= since)
               .scalar())
        return float(v) if v is not None else None

    # Unique visitors today.
    today = date.today()
    unique_today = (db.query(func.count(VisitorTrack.id))
                      .filter(VisitorTrack.store_id == store_id,
                              VisitorTrack.day == today)
                      .scalar() or 0)

    # Alert volume by type (last `days` days).
    alert_breakdown = dict(
        db.query(DetectionEvent.detection_type, func.count(Alert.id))
          .join(Alert, Alert.event_id == DetectionEvent.id)
          .join(Camera, Camera.id == DetectionEvent.camera_id)
          .filter(Camera.store_id == store_id,
                  Alert.created_at >= since)
          .group_by(DetectionEvent.detection_type)
          .all()
    )

    return {
        "store_id": store.id,
        "store_name": store.name,
        "country": store.country,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "kpis": {
            "occupancy_now":        _last("occupancy"),
            "occupancy_avg":        _avg("occupancy"),
            "queue_length_now":     _last("queue_length"),
            "queue_length_avg":     _avg("queue_length"),
            "queue_wait_avg_sec":   _avg("queue_wait_seconds"),
            "staff_present_avg":    _avg("staff_present_pct"),
            "unique_visitors_today": int(unique_today),
            "passersby_avg":        _avg("passersby"),
            "stop_rate_avg":        _avg("stop_rate"),
            "dwell_seconds_avg":    _avg("dwell_seconds"),
            "shutter_open_now":     _last("shutter_open"),
        },
        "alerts_breakdown": alert_breakdown,
    }


@router.get("/dashboard/multi")
def multi_store(db: Session = Depends(get_db), _u=Depends(get_current_user),
                days: int = 7):
    """Aggregate KPIs across all stores — used by the head-office page."""
    stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
    rows = []
    for s in stores:
        row = store_dashboard(s.id, days=days, db=db, _u=_u)
        rows.append(row)
    # Totals across the chain.
    totals = {
        "unique_visitors_today": sum((r["kpis"]["unique_visitors_today"] or 0) for r in rows),
        "stores":                len(rows),
        "alerts_total":          sum(sum(r["alerts_breakdown"].values()) for r in rows),
    }
    return {"stores": rows, "totals": totals}
