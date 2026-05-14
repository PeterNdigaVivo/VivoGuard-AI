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

    # JOIN through cameras.store_id rather than filtering
    # MetricSnapshot.store_id directly. Why: a metric row written when
    # the camera was unattached has store_id=NULL; after the operator
    # attaches the camera, those rows would otherwise stay invisible to
    # the dashboard forever. Joining through cameras picks them up
    # automatically.
    def _last(metric: str) -> float | None:
        row = (db.query(MetricSnapshot)
                 .join(Camera, Camera.id == MetricSnapshot.camera_id)
                 .filter(Camera.store_id == store_id,
                         MetricSnapshot.metric_type == metric)
                 .order_by(MetricSnapshot.period_start.desc())
                 .first())
        return row.value if row else None

    def _avg(metric: str) -> float | None:
        v = (db.query(func.avg(MetricSnapshot.value))
               .join(Camera, Camera.id == MetricSnapshot.camera_id)
               .filter(Camera.store_id == store_id,
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= since)
               .scalar())
        return float(v) if v is not None else None

    def _sum(metric: str) -> float | None:
        v = (db.query(func.sum(MetricSnapshot.value))
               .join(Camera, Camera.id == MetricSnapshot.camera_id)
               .filter(Camera.store_id == store_id,
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
            # Directional entry/exit counters (Lumana parity, P1).
            "visitors_in_window":   _sum("visitor_count_in"),
            "visitors_out_window":  _sum("visitor_count_out"),
            "visitors_net_window":  ((_sum("visitor_count_in") or 0)
                                     - (_sum("visitor_count_out") or 0)),
            "passersby_avg":        _avg("passersby"),
            "stop_rate_avg":        _avg("stop_rate"),
            "dwell_seconds_avg":    _avg("dwell_seconds"),
            "shutter_open_now":     _last("shutter_open"),
        },
        "alerts_breakdown": alert_breakdown,
    }


# ---- Heatmap export (P2) -------------------------------------------

@router.get("/heatmap/{camera_id}")
def heatmap_grid(camera_id: int, _u=Depends(get_current_user)):
    """Return the latest heatmap as a 2-D array of cell counts."""
    import json
    import redis
    from app.config import settings
    r = redis.from_url(settings.redis_url)
    raw = r.get(f"vg:heatmap:{camera_id}")
    if not raw:
        raise HTTPException(404, "heatmap not available — is heatmap detection enabled?")
    return json.loads(raw)


@router.get("/heatmap/{camera_id}/image")
def heatmap_image(camera_id: int, alpha: float = 0.65, _u=Depends(get_current_user)):
    """Render the heatmap as a PNG with a jet-style colour ramp.

    The frontend overlays this on the camera snapshot (or on a manually
    uploaded floorplan) at the operator's chosen opacity. Returns
    `image/png` bytes.
    """
    import io
    import json
    import redis
    from fastapi.responses import StreamingResponse
    from app.config import settings

    r = redis.from_url(settings.redis_url)
    raw = r.get(f"vg:heatmap:{camera_id}")
    if not raw:
        raise HTTPException(404, "heatmap not available")
    payload = json.loads(raw)
    grid = payload.get("grid") or []
    n    = payload.get("size") or len(grid) or 32
    if not grid:
        raise HTTPException(404, "heatmap empty")

    from PIL import Image
    max_v = max((max(row) for row in grid), default=0) or 1
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    px = img.load()
    for y in range(n):
        for x in range(n):
            v = grid[y][x] / max_v
            if v <= 0:
                continue
            # Simple "blue-cyan-yellow-red" ramp.
            if v < 0.25:
                rgb = (0, int(v * 4 * 255), 255)
            elif v < 0.5:
                rgb = (0, 255, int((1 - (v - 0.25) * 4) * 255))
            elif v < 0.75:
                rgb = (int((v - 0.5) * 4 * 255), 255, 0)
            else:
                rgb = (255, int((1 - (v - 0.75) * 4) * 255), 0)
            px[x, y] = (*rgb, int(alpha * 255))
    # Upscale 16× for a smoother overlay; bilinear keeps the look soft.
    img = img.resize((n * 16, n * 16), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ---- Peak-hour staffing recommendations (P3) -----------------------

@router.get("/staffing-recommendation/{store_id}")
def staffing_recommendation(store_id: int, days: int = 14,
                            target_ratio: float = 0.08,
                            db: Session = Depends(get_db),
                            _u=Depends(get_current_user)):
    """Analyse the last N days of `occupancy` metrics for the store,
    bucket by hour-of-day, and recommend a staff headcount per bucket
    using `target_ratio` (staff per visitor — default 1 staff per
    ~12 visitors). Returns up to 24 rows sorted by hour."""
    from sqlalchemy import extract
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(
                extract("hour", MetricSnapshot.period_start).label("hour"),
                func.avg(MetricSnapshot.value).label("avg_occ"),
                func.max(MetricSnapshot.value).label("peak_occ"),
            )
            .join(Camera, Camera.id == MetricSnapshot.camera_id)
            .filter(Camera.store_id == store_id,
                    MetricSnapshot.metric_type == "occupancy",
                    MetricSnapshot.period_start >= since)
            .group_by("hour")
            .order_by("hour")
            .all())
    out = []
    for hr, avg_occ, peak_occ in rows:
        avg = float(avg_occ or 0)
        peak = float(peak_occ or 0)
        out.append({
            "hour": int(hr),
            "avg_occupancy":  round(avg, 1),
            "peak_occupancy": round(peak, 1),
            "recommended_staff": max(1, int(round(peak * target_ratio))),
        })
    return {"store_id": store_id, "days_analysed": days,
            "target_staff_per_visitor": target_ratio, "by_hour": out}


# ---- Side-by-side store comparison (P3) ----------------------------

@router.get("/compare")
def compare_stores(metric_type: str = "occupancy",
                   days: int = 7,
                   db: Session = Depends(get_db),
                   _u=Depends(get_current_user)):
    """Returns one row per active store with avg / peak / total / sample
    count for the given metric. Powers the chain comparison chart."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(
                Store.id, Store.name, Store.country,
                func.avg(MetricSnapshot.value).label("avg_v"),
                func.max(MetricSnapshot.value).label("peak_v"),
                func.sum(MetricSnapshot.value).label("sum_v"),
                func.count(MetricSnapshot.id).label("samples"),
            )
            .join(Camera, Camera.store_id == Store.id)
            .join(MetricSnapshot, MetricSnapshot.camera_id == Camera.id)
            .filter(MetricSnapshot.metric_type == metric_type,
                    MetricSnapshot.period_start >= since,
                    Store.is_active == True)  # noqa: E712
            .group_by(Store.id, Store.name, Store.country)
            .order_by(func.avg(MetricSnapshot.value).desc())
            .all())
    return {
        "metric_type": metric_type, "days": days,
        "stores": [
            {"store_id": sid, "store_name": name, "country": country,
             "avg":   float(avg_v or 0),
             "peak":  float(peak_v or 0),
             "total": float(sum_v or 0),
             "samples": int(samples)}
            for (sid, name, country, avg_v, peak_v, sum_v, samples) in rows
        ],
    }


# ---- Customer journeys (P3) ----------------------------------------

@router.get("/journeys/{store_id}")
def journeys_for_store(store_id: int, days: int = 1,
                       limit: int = 200,
                       db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    from app.models import CustomerJourney
    from datetime import date
    since_day = date.today() - timedelta(days=days - 1)
    rows = (db.query(CustomerJourney)
              .filter(CustomerJourney.store_id == store_id,
                      CustomerJourney.day >= since_day)
              .order_by(CustomerJourney.started_at.desc())
              .limit(limit).all())
    from collections import Counter
    seq_counter: Counter = Counter()
    for j in rows:
        seq = tuple((z.get("zone_name") or f"z{z.get('zone_id')}")
                    for z in (j.zone_sequence_json or []))
        if len(seq) >= 2:
            seq_counter[seq] += 1
    top_paths = [{"path": list(seq), "count": cnt}
                 for seq, cnt in seq_counter.most_common(10)]
    return {
        "store_id": store_id,
        "journey_count": len(rows),
        "top_paths": top_paths,
        "journeys": [
            {"id": j.id, "started_at": j.started_at.isoformat(),
             "ended_at": j.ended_at.isoformat() if j.ended_at else None,
             "zones": j.zone_sequence_json}
            for j in rows[:50]
        ],
    }


# ---- Backfill metric_snapshots.store_id from current camera.store_id ----

@router.post("/admin/backfill-store-ids")
def backfill_store_ids(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """One-shot fixer. Sets metric_snapshots.store_id = cameras.store_id
    for every metric row currently NULL whose camera is now attached to
    a store. The dashboard JOINs through cameras anyway, but writing it
    down makes per-store SQL queries cheaper and helps the PDF reports.
    Safe to re-run."""
    from sqlalchemy import text
    res = db.execute(text("""
        UPDATE metric_snapshots m
        SET store_id = c.store_id
        FROM cameras c
        WHERE m.camera_id = c.id
          AND m.store_id IS NULL
          AND c.store_id IS NOT NULL
        RETURNING m.id
    """))
    n = len(res.fetchall())
    db.commit()
    return {"backfilled_rows": n}


# ---- Campaign analytics (P4) ---------------------------------------

@router.get("/campaigns/{campaign_id}/lift")
def campaign_lift_endpoint(campaign_id: int,
                           metric_type: str = "passersby",
                           db: Session = Depends(get_db),
                           _u=Depends(get_current_user)):
    """Compare same-length windows before / during / after the campaign."""
    from app.analytics.reports import campaign_lift
    try:
        return campaign_lift(db, campaign_id, metric_type=metric_type)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ---- Exportable reports (P4) ---------------------------------------

@router.get("/report.csv")
def report_csv(since: datetime, until: datetime,
               store_id: int | None = None,
               db: Session = Depends(get_db),
               _u=Depends(get_current_user)):
    from fastapi.responses import StreamingResponse as _Stream
    from app.analytics.reports import store_rollup, stores_csv
    if store_id:
        ids = [store_id]
    else:
        ids = [s.id for s in db.query(Store).filter(Store.is_active == True).all()]  # noqa: E712
    rollups = [store_rollup(db, sid, since=since, until=until) for sid in ids]
    payload = stores_csv(rollups)
    return _Stream(iter([payload]), media_type="text/csv",
                   headers={"Content-Disposition": 'attachment; filename="vivoguard_report.csv"'})


@router.get("/report.pdf")
def report_pdf(since: datetime, until: datetime,
               store_id: int | None = None,
               db: Session = Depends(get_db),
               _u=Depends(get_current_user)):
    from fastapi.responses import StreamingResponse as _Stream
    from app.analytics.reports import store_rollup, stores_pdf
    if store_id:
        ids = [store_id]
        title = f"VivoGuard — store #{store_id} report"
    else:
        ids = [s.id for s in db.query(Store).filter(Store.is_active == True).all()]  # noqa: E712
        title = "VivoGuard — chain report"
    rollups = [store_rollup(db, sid, since=since, until=until) for sid in ids]
    pdf = stores_pdf(title, rollups)
    return _Stream(iter([pdf]), media_type="application/pdf",
                   headers={"Content-Disposition": 'attachment; filename="vivoguard_report.pdf"'})


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
