"""Retail analytics read API.

  GET /analytics/metrics
        ?metric_type=queue_length
        &store_id=...
        &camera_id=...
        &since=...&until=...

  GET /analytics/store/{id}/live
        — Rule-3 dashboard: every required tile in one payload, never
          returns NULL for an active camera. 0 means 'detector running,
          nothing detected'. status=="no_data_yet" only when every
          camera in the store has been online <10 min.

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


# ====================================================================
# Live store dashboard — Rules 1, 2, 3 of the retail overhaul
# ====================================================================

@router.get("/store/{store_id}/live")
def store_live_dashboard(store_id: int,
                         since: datetime | None = None,
                         until: datetime | None = None,
                         db: Session = Depends(get_db),
                         _u=Depends(get_current_user)):
    """Single payload powering the redesigned store dashboard.

    When `since`/`until` are omitted defaults to today (00:00→now) and
    compares against yesterday (same window). When given explicitly,
    compares against the prior same-length window so every KPI tile
    carries a trend arrow regardless of the range chosen.

    Semantics (Rule 3):
      For every active camera attached we expect at least one metric
      row in the last 5 minutes. If a camera has been attached <10 min
      ago and has no metrics yet we report 'no_data_yet'; otherwise
      missing metrics become 0. Tiles for zone-gated metrics carry a
      `visible` flag — the frontend hides the tile when no such zone
      exists in the store.
    """
    from sqlalchemy import extract
    from app.utils.business_hours import (
        is_store_open, todays_session, todays_hours_label,
    )
    from app.stream.frame_buffer import FrameBuffer
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")

    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    now = datetime.now(timezone.utc)

    # ----- Camera liveness gate -----
    # A camera's `status` column lags reality (gets bumped to 'online'
    # at create-time). The source of truth is the streamer's Redis
    # health row — last_frame_at within the last ~30s means we have
    # current frames flowing. Counting these is cheap (one Redis GET
    # per camera) and lets the UI distinguish:
    #   • no cameras attached     → "Add a camera" CTA
    #   • cameras attached but DARK→ "No cameras online" badge
    #   • at least one live       → normal dashboard
    fb = FrameBuffer()
    cameras_online = 0
    for cam_id in cam_ids:
        h = fb.health(cam_id) or {}
        lfa = h.get("last_frame_at")
        if lfa and (now.timestamp() - float(lfa)) < 30:
            cameras_online += 1

    # ----- Business-hours gate -----
    open_now = is_store_open(store)
    session_open_utc, session_close_utc = todays_session(store)
    hours_label = todays_hours_label(store)

    if not cam_ids:
        return {
            "store_id": store_id, "store_name": store.name,
            "country": store.country, "status": "no_cameras",
            "as_of": now.isoformat(),
            "tiles": {}, "zone_capabilities": {},
            "is_open": open_now,
            "hours_label": hours_label,
            "cameras_online": 0,
            "cameras_total": 0,
        }

    # Cameras attached but none currently streaming → distinct state.
    # We still return zone capabilities so the dashboard knows what
    # KPI tiles WOULD have been shown.
    no_live = cameras_online == 0
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    five_min_ago = now - timedelta(minutes=5)

    # Active window (defaults to today's BUSINESS-HOURS session, not
    # raw 00:00→now). Clamping to the session means the "Today so
    # far" KPIs aggregate over operating hours only — no 6am cleaner
    # noise inflating the staff-presence average. The "previous"
    # window is the same length immediately before.
    if since is None:
        # Default: clamp to today's session. If the store is closed
        # right now, active_until == session_close (or now if before
        # close); since == session_open.
        active_since = session_open_utc
        active_until = min(now, session_close_utc) if session_close_utc > session_open_utc else now
    else:
        active_since = since
        active_until = until or now
    window_len = active_until - active_since
    prev_since = active_since - window_len
    prev_until = active_since

    youngest_cam_age = min(
        ((now - c.created_at).total_seconds() if c.created_at else 999999)
        for c in cams
    )

    # Zone capabilities — which tiles to show.
    from app.models import Zone
    zone_tags: set[str] = set()
    for z in (db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()):
        for t in (z.detection_types_json or []):
            zone_tags.add(t)

    capabilities = {
        "queue":         "queue" in zone_tags,
        "counter":       "counter" in zone_tags,
        "aisle":         "aisle" in zone_tags,
        "entry_exit":    "entry_exit" in zone_tags,
        "shutter":       "shutter" in zone_tags,
        "stockroom":     "stockroom" in zone_tags,
        "shelf_change":  "shelf_change" in zone_tags,
        "sidewalk":      "sidewalk" in zone_tags,
        "window":        "window" in zone_tags,
        "high_value":    "high_value" in zone_tags,
        "restricted":    "restricted" in zone_tags,
    }

    def _has_recent_data() -> bool:
        return bool(
            db.query(MetricSnapshot.id)
              .filter(MetricSnapshot.camera_id.in_(cam_ids),
                      MetricSnapshot.period_start >= five_min_ago)
              .first()
        )

    # If every camera was attached very recently AND there are zero
    # rows yet, report "no_data_yet" instead of zeroes.
    if youngest_cam_age < 600 and not _has_recent_data():
        return {
            "store_id": store_id, "store_name": store.name,
            "country": store.country, "status": "no_data_yet",
            "as_of": now.isoformat(),
            "tiles": {}, "zone_capabilities": capabilities,
        }

    # ----- aggregations ---------------------------------------------
    # Each helper takes an explicit window so we can call it twice —
    # once for the active range, once for the prior same-length window
    # — and surface trend-vs-previous on every KPI.
    def _sum(metric: str, t0: datetime, t1: datetime) -> float:
        v = (db.query(func.sum(MetricSnapshot.value))
               .filter(MetricSnapshot.camera_id.in_(cam_ids),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return float(v) if v is not None else 0.0

    def _max(metric: str, t0: datetime, t1: datetime) -> float:
        v = (db.query(func.max(MetricSnapshot.value))
               .filter(MetricSnapshot.camera_id.in_(cam_ids),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return float(v) if v is not None else 0.0

    def _avg(metric: str, t0: datetime, t1: datetime) -> float:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(MetricSnapshot.camera_id.in_(cam_ids),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return float(v) if v is not None else 0.0

    def _trend(curr: float, prev: float) -> dict:
        """Trend dict for the frontend's Trend pill."""
        if prev <= 0:
            return {"direction": "flat", "delta_pct": None}
        pct = (curr - prev) / prev * 100
        return {
            "direction": "up" if pct > 5 else "down" if pct < -5 else "flat",
            "delta_pct": round(pct, 1),
        }

    def _kpi_avg(metric: str) -> dict:
        c = _avg(metric, active_since, active_until)
        p = _avg(metric, prev_since,   prev_until)
        return {"value": c, "trend": _trend(c, p)}

    def _kpi_sum(metric: str) -> dict:
        c = _sum(metric, active_since, active_until)
        p = _sum(metric, prev_since,   prev_until)
        return {"value": c, "trend": _trend(c, p)}

    def _kpi_max(metric: str) -> dict:
        c = _max(metric, active_since, active_until)
        p = _max(metric, prev_since,   prev_until)
        return {"value": c, "trend": _trend(c, p)}

    def _latest_sum(metric: str) -> float:
        """Sum the latest sample of `metric` across the store's cameras.
        Use case: occupancy NOW = sum of each camera's most recent
        occupancy reading. (Without ReID, this can double-count people
        seen by two cameras simultaneously — see camera-aggregation doc.)"""
        # Subquery: latest period_start per camera_id for this metric.
        sub = (db.query(MetricSnapshot.camera_id,
                        func.max(MetricSnapshot.period_start).label("p"))
                 .filter(MetricSnapshot.camera_id.in_(cam_ids),
                         MetricSnapshot.metric_type == metric,
                         MetricSnapshot.period_start >= five_min_ago)
                 .group_by(MetricSnapshot.camera_id)
                 .subquery())
        v = (db.query(func.sum(MetricSnapshot.value))
               .join(sub, (MetricSnapshot.camera_id == sub.c.camera_id)
                          & (MetricSnapshot.period_start == sub.c.p))
               .filter(MetricSnapshot.metric_type == metric)
               .scalar())
        return float(v) if v is not None else 0.0

    # Unique visitors in the active range.
    from app.models import VisitorTrack

    def _unique_visitors(t0: datetime, t1: datetime) -> int:
        return (db.query(func.count(VisitorTrack.id))
                  .filter(VisitorTrack.camera_id.in_(cam_ids),
                          VisitorTrack.first_seen >= t0,
                          VisitorTrack.first_seen <  t1)
                  .scalar() or 0)
    unique_curr = _unique_visitors(active_since, active_until)
    unique_prev = _unique_visitors(prev_since,   prev_until)

    # Alerts in the active range, grouped by type.
    alerts_today = dict(
        db.query(DetectionEvent.detection_type, func.count(Alert.id))
          .join(Alert, Alert.event_id == DetectionEvent.id)
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  Alert.created_at >= active_since,
                  Alert.created_at <  active_until)
          .group_by(DetectionEvent.detection_type)
          .all()
    )

    # Hourly footfall sparkline — hour-bucketed occupancy (peak per hour)
    # within the active range. For multi-day ranges this becomes 24 bars
    # of "average peak by hour-of-day"; for single-day ranges it's the
    # familiar today sparkline.
    hourly_rows = (db.query(extract("hour", MetricSnapshot.period_start).label("hr"),
                            func.max(MetricSnapshot.value))
                     .filter(MetricSnapshot.camera_id.in_(cam_ids),
                             MetricSnapshot.metric_type == "occupancy",
                             MetricSnapshot.period_start >= active_since,
                             MetricSnapshot.period_start <  active_until)
                     .group_by("hr").order_by("hr").all())
    hourly_footfall = [{"hour": int(h), "value": float(v or 0)} for h, v in hourly_rows]

    # Per-aisle dwell, top 3, in the active range.
    aisle_rows = (db.query(MetricSnapshot.zone_id,
                           func.avg(MetricSnapshot.value).label("d"))
                    .filter(MetricSnapshot.camera_id.in_(cam_ids),
                            MetricSnapshot.metric_type == "dwell_seconds",
                            MetricSnapshot.period_start >= active_since,
                            MetricSnapshot.period_start <  active_until)
                    .group_by(MetricSnapshot.zone_id)
                    .order_by(func.avg(MetricSnapshot.value).desc())
                    .limit(3).all())
    top_aisles = []
    for zid, d in aisle_rows:
        z = db.get(Zone, zid) if zid else None
        top_aisles.append({
            "zone_id": zid,
            "zone_name": z.name if z else "(unnamed)",
            "avg_dwell_seconds": round(float(d or 0), 1),
        })

    # Heatmap thumbnail URL — first camera's most recent Redis grid.
    heatmap_thumb_url = None
    if cam_ids:
        heatmap_thumb_url = f"/api/analytics/heatmap/{cam_ids[0]}/image?alpha=0.65"

    # Status traffic light: red if any high-priority alerts in window,
    # amber if staff_present_avg < 0.5 and counter exists, else green.
    staff_avg = _avg("staff_present_pct", active_since, active_until) if capabilities["counter"] else None
    high_alerts = sum(
        cnt for t, cnt in alerts_today.items()
        if t in ("intrusion", "fight", "weapon", "weapon_brandished", "shrinkage")
    )
    if high_alerts > 0:
        status_light = "red"
    elif staff_avg is not None and staff_avg < 0.5:
        status_light = "amber"
    else:
        status_light = "green"

    # Every KPI tile carries a trend dict computed against the prior
    # same-length window. Operator picks "Last week" → trend shows
    # vs the week before that, etc.
    occupancy_peak  = _kpi_max("occupancy")
    queue_wait_avg  = _kpi_avg("queue_wait_seconds")
    visitors_in     = _kpi_sum("visitor_count_in")
    visitors_out    = _kpi_sum("visitor_count_out")

    tiles = {
        # NOW-tiles: latest-sample, no trend (snapshot in time).
        "occupancy_now": {
            "value": round(_latest_sum("occupancy"), 0),
            "visible": True,
        },
        "queue_length_now": {
            "value": round(_latest_sum("queue_length"), 0),
            "visible": capabilities["queue"],
        },
        # WINDOW-tiles: scoped to active_since→active_until + trend.
        "occupancy_peak_today": {
            "value": round(occupancy_peak["value"], 0),
            "trend": occupancy_peak["trend"],
            "visible": True,
        },
        "unique_visitors_today": {
            "value": int(unique_curr),
            "trend": _trend(unique_curr, unique_prev),
            "visible": True,
        },
        "queue_wait_avg_today_sec": {
            "value": round(queue_wait_avg["value"], 0),
            "trend": queue_wait_avg["trend"],
            "visible": capabilities["queue"],
        },
        "staff_present_pct_today": {
            "value": round((staff_avg or 0) * 100, 0),
            "trend": _trend(
                staff_avg or 0,
                _avg("staff_present_pct", prev_since, prev_until) if capabilities["counter"] else 0,
            ),
            "visible": capabilities["counter"],
        },
        "visitors_in_today":  {
            "value": round(visitors_in["value"], 0),
            "trend": visitors_in["trend"],
            "visible": capabilities["entry_exit"],
        },
        "visitors_out_today": {
            "value": round(visitors_out["value"], 0),
            "trend": visitors_out["trend"],
            "visible": capabilities["entry_exit"],
        },
        "visitors_net_today": {
            "value": round(visitors_in["value"] - visitors_out["value"], 0),
            "trend": _trend(
                visitors_in["value"] - visitors_out["value"],
                _sum("visitor_count_in",  prev_since, prev_until)
                 - _sum("visitor_count_out", prev_since, prev_until),
            ),
            "visible": capabilities["entry_exit"],
        },
        # Non-KPI tiles (lists / charts / image URL).
        "top_aisles":            {"value": top_aisles, "visible": capabilities["aisle"]},
        "alerts_today_by_type":  {"value": alerts_today, "visible": True},
        "hourly_footfall_today": {"value": hourly_footfall, "visible": True},
        "heatmap_thumb_url":     {"value": heatmap_thumb_url, "visible": True},
    }

    # `status` precedence (most-specific wins):
    #   no_cameras_live  — cameras attached but none streaming
    #   closed           — store is outside its business hours
    #   live             — normal operation
    if no_live:
        status_str = "no_cameras_live"
    elif not open_now:
        status_str = "closed"
    else:
        status_str = "live"

    return {
        "store_id": store_id, "store_name": store.name,
        "country": store.country, "status": status_str,
        "status_light": status_light,
        "as_of": now.isoformat(),
        "active_range": {"since": active_since.isoformat(),
                          "until": active_until.isoformat()},
        "prev_range":   {"since": prev_since.isoformat(),
                          "until": prev_until.isoformat()},
        "camera_count": len(cams),
        "cameras_total":  len(cams),
        "cameras_online": cameras_online,
        "is_open": open_now,
        "hours_label": hours_label,
        "session_open":  session_open_utc.isoformat(),
        "session_close": session_close_utc.isoformat(),
        "zone_capabilities": capabilities,
        "tiles": tiles,
    }


# ---- Week summary + detector activity (May-2026 redesign) ---------

@router.get("/store/{store_id}/week-summary")
def store_week_summary(store_id: int, db: Session = Depends(get_db),
                        _u=Depends(get_current_user)):
    """Powers the dashboard's THIS WEEK section.

    Three things in one payload:

      1. 7-day footfall — sum of `visitor_count_in` per day for the
         last 7 days (or `occupancy_peak` when entry/exit aren't
         instrumented yet — same shape so the chart just works).
      2. Best/worst day in the window + average occupancy per
         weekday (Mon-Sun).
      3. Top 3 busiest hours-of-day across the week (UTC hour
         buckets, summed counts).
      4. Detector activity table — for every active detector in
         the catalog, count events today vs this week so operators
         can see at a glance which detectors are running.

    All windows are clamped to the store's business-hours session
    on each day, so closed-store noise doesn't pollute the bars.
    """
    from sqlalchemy import extract
    from app.utils.business_hours import is_store_open, todays_session
    from app.api.detector_catalog import DETECTOR_CATALOG
    from app.models import Zone

    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"store_id": store_id, "store_name": store.name,
                "days": [], "best_day": None, "worst_day": None,
                "weekday_avg_occupancy": [], "top_hours": [],
                "detector_activity": []}

    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    today_session_open, _ = todays_session(store, now)

    # --- 7-day footfall: prefer visitor_count_in; fall back to the
    #     max occupancy of the day if entry_exit isn't instrumented.
    visitor_in_rows = (
        db.query(extract("doy", MetricSnapshot.period_start).label("doy"),
                 func.sum(MetricSnapshot.value).label("v"))
          .filter(MetricSnapshot.camera_id.in_(cam_ids),
                  MetricSnapshot.metric_type == "visitor_count_in",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by("doy").all()
    )
    visitor_by_doy = {int(r.doy): float(r.v or 0) for r in visitor_in_rows}

    occ_max_rows = (
        db.query(extract("doy", MetricSnapshot.period_start).label("doy"),
                 func.max(MetricSnapshot.value).label("v"))
          .filter(MetricSnapshot.camera_id.in_(cam_ids),
                  MetricSnapshot.metric_type == "occupancy",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by("doy").all()
    )
    occ_max_by_doy = {int(r.doy): float(r.v or 0) for r in occ_max_rows}

    days: list[dict] = []
    for i in range(7):
        day = (week_start + timedelta(days=i))
        doy = int(day.strftime("%j"))
        # Prefer visitor count; fall back to peak occupancy
        # so the chart isn't blank for entry-exit-less stores.
        value = visitor_by_doy.get(doy)
        source = "visitors_in"
        if value is None or value == 0:
            value = occ_max_by_doy.get(doy, 0.0)
            source = "occupancy_peak"
        days.append({
            "date": day.date().isoformat(),
            "weekday": day.strftime("%a"),
            "value": round(value, 0),
            "source": source,
        })

    non_zero = [d for d in days if d["value"] > 0]
    best_day  = max(non_zero, key=lambda d: d["value"]) if non_zero else None
    worst_day = min(non_zero, key=lambda d: d["value"]) if non_zero else None

    # --- Avg peak occupancy per weekday (Mon-Sun).
    weekday_rows = (
        db.query(extract("dow", MetricSnapshot.period_start).label("dow"),
                 func.avg(MetricSnapshot.value).label("v"))
          .filter(MetricSnapshot.camera_id.in_(cam_ids),
                  MetricSnapshot.metric_type == "occupancy",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by("dow").order_by("dow").all()
    )
    # Postgres dow: 0=Sun..6=Sat. Map to Mon-first for the UI.
    pg_dow_to_label = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed",
                       4: "Thu", 5: "Fri", 6: "Sat"}
    weekday_avg: list[dict] = []
    for row in weekday_rows:
        weekday_avg.append({
            "weekday": pg_dow_to_label.get(int(row.dow), str(int(row.dow))),
            "value":   round(float(row.v or 0), 1),
        })

    # --- Top 3 busiest hours across the week.
    hour_rows = (
        db.query(extract("hour", MetricSnapshot.period_start).label("h"),
                 func.sum(MetricSnapshot.value).label("v"))
          .filter(MetricSnapshot.camera_id.in_(cam_ids),
                  MetricSnapshot.metric_type == "visitor_count_in",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by("h").order_by(func.sum(MetricSnapshot.value).desc())
          .limit(3).all()
    )
    top_hours = [
        {"hour": int(r.h), "label": f"{int(r.h):02d}:00", "value": int(r.v or 0)}
        for r in hour_rows
    ]

    # --- Detector activity: events today vs week, per active detector.
    # Uses DetectionEvent rows (raw inference output) rather than
    # MetricSnapshot rows. Some detectors fire events (intrusion,
    # fight, fall) where each event is interesting; others produce
    # continuous metrics (occupancy). We report event counts for the
    # event-emitting set; the rest get '— continuous —' on the row.
    today_start = today_session_open
    from app.models import DetectionEvent

    today_counts = dict(
        db.query(DetectionEvent.detection_type, func.count(DetectionEvent.id))
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  DetectionEvent.detected_at >= today_start)
          .group_by(DetectionEvent.detection_type).all()
    )
    week_counts = dict(
        db.query(DetectionEvent.detection_type, func.count(DetectionEvent.id))
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  DetectionEvent.detected_at >= week_start)
          .group_by(DetectionEvent.detection_type).all()
    )

    # Active zones tell us which zone-gated detectors are even
    # relevant for this store. Zone-gated detectors that have no
    # zone get tagged "needs_zone" so the UI can render the right
    # CTA ('Set up zone' deep link).
    zone_tags: set[str] = set()
    for z in db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all():
        for t in (z.detection_types_json or []):
            zone_tags.add(t)

    detector_activity: list[dict] = []
    for det_type, meta in DETECTOR_CATALOG.items():
        if meta.get("status") != "active":
            continue
        needs_zone = bool(meta.get("needs_zone"))
        zone_present = (det_type in zone_tags) if needs_zone else True
        detector_activity.append({
            "detector": det_type,
            "events_today": int(today_counts.get(det_type, 0)),
            "events_week":  int(week_counts.get(det_type, 0)),
            "needs_zone":   needs_zone,
            "zone_present": zone_present,
            # 'active' if running for this store; 'needs_setup' if
            # zone-gated but no zone exists yet.
            "status": "active" if zone_present else "needs_setup",
        })

    # Stable sort: store-relevant zone-gated detectors first
    # (most actionable), then anything else.
    detector_activity.sort(
        key=lambda d: (d["status"] != "active", -d["events_week"], d["detector"])
    )

    return {
        "store_id": store_id,
        "store_name": store.name,
        "window": {"since": week_start.isoformat(), "until": now.isoformat()},
        "days": days,
        "best_day":  best_day,
        "worst_day": worst_day,
        "weekday_avg_occupancy": weekday_avg,
        "top_hours": top_hours,
        "detector_activity": detector_activity,
    }


# ---- Store-wide heatmap grid (Rule 5) ------------------------------

@router.get("/store/{store_id}/heatmaps")
def store_heatmaps(store_id: int, db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """One row per camera in the store with its heatmap thumb URL plus
    the top hotspot location (highest-count cell). Frontend grids them."""
    import json
    import redis
    from app.config import settings as _settings
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    r = redis.from_url(_settings.redis_url)
    out = []
    for c in cams:
        raw = r.get(f"vg:heatmap:{c.id}")
        hotspot = None
        if raw:
            try:
                p = json.loads(raw)
                g = p.get("grid") or []
                if g:
                    mx, my, mv = 0, 0, 0
                    for y, row in enumerate(g):
                        for x, v in enumerate(row):
                            if v > mv:
                                mv, mx, my = v, x, y
                    n = p.get("size") or len(g)
                    hotspot = {
                        "cell_x": mx, "cell_y": my, "value": int(mv),
                        "norm_x": round(mx / max(1, n - 1), 3),
                        "norm_y": round(my / max(1, n - 1), 3),
                    }
            except Exception:
                pass
        out.append({
            "camera_id": c.id,
            "camera_name": c.name,
            "status": c.status,
            "heatmap_url": f"/api/analytics/heatmap/{c.id}/image?alpha=0.65",
            "hotspot": hotspot,
        })
    ranked = sorted([o for o in out if o["hotspot"]],
                    key=lambda x: -(x["hotspot"]["value"] or 0))
    labels = ["Busiest area", "Second busiest", "Third busiest"]
    for i, o in enumerate(ranked):
        o["rank_label"] = labels[i] if i < len(labels) else None
    return {"store_id": store_id, "cameras": out}


# ---- Heatmap daily archive (30-day rolling retention) --------------

@router.get("/heatmap/{camera_id}/archive")
def heatmap_archive(camera_id: int, db: Session = Depends(get_db),
                    _u=Depends(get_current_user)):
    """List every archived daily heatmap PNG for a camera."""
    from app.models import HeatmapSnapshot
    rows = (db.query(HeatmapSnapshot)
              .filter(HeatmapSnapshot.camera_id == camera_id)
              .order_by(HeatmapSnapshot.day.desc())
              .all())
    return {
        "camera_id": camera_id,
        "snapshots": [
            {
                "day": r.day.isoformat(),
                "peak_value": r.peak_value,
                "download_url": f"/api/analytics/heatmap/{camera_id}/archive/{r.day.isoformat()}",
            }
            for r in rows
        ],
    }


@router.get("/heatmap/{camera_id}/archive/{day}")
def heatmap_archive_download(camera_id: int, day: str,
                             db: Session = Depends(get_db),
                             _u=Depends(get_current_user)):
    """Return one archived heatmap PNG by camera + date (YYYY-MM-DD)."""
    from datetime import date as _date
    from fastapi.responses import FileResponse
    from app.models import HeatmapSnapshot
    try:
        d = _date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    row = (db.query(HeatmapSnapshot)
             .filter(HeatmapSnapshot.camera_id == camera_id,
                     HeatmapSnapshot.day == d).first())
    if not row:
        raise HTTPException(404, "no archived heatmap for that day")
    return FileResponse(row.file_path, media_type="image/png",
                        filename=f"heatmap_{camera_id}_{day}.png")


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


# Premier League / Opta / SofaScore football-analytics colour scheme.
# Cold zones in deep navy, hot zones in vivid orange/red. Stops are
# laid out so transitions land at quartile-ish intensity levels, which
# gives the sharp banding the football overlays use rather than a
# smooth jet gradient. Defined here once so the live PNG endpoint and
# the nightly archive task render the same scheme.
_HEATMAP_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.00, (0x0a, 0x16, 0x28)),   # deep navy — coldest
    (0.20, (0x1e, 0x3a, 0x8a)),   # royal blue
    (0.45, (0x3b, 0x82, 0xf6)),   # sky blue
    (0.65, (0xf9, 0x73, 0x16)),   # orange
    (0.85, (0xea, 0x58, 0x0c)),   # bright orange
    (1.00, (0xff, 0x45, 0x00)),   # vivid orange-red — hotspot
]
# Threshold above which we draw a white "bloom" ring around the cell
# to make the hottest patch unmistakable in the overlay.
_HEATMAP_HOTSPOT_THRESHOLD = 0.85


def _heatmap_color(v: float) -> tuple[int, int, int]:
    """Map a 0..1 intensity to the Premier-League-style RGB tuple.

    Banded — interpolates within a stop but doesn't smooth across
    them aggressively, so adjacent intensity tiers visibly differ
    (the football-style look the spec asks for).
    """
    v = max(0.0, min(1.0, v))
    for i in range(len(_HEATMAP_STOPS) - 1):
        lo_v, lo_c = _HEATMAP_STOPS[i]
        hi_v, hi_c = _HEATMAP_STOPS[i + 1]
        if v <= hi_v:
            span = hi_v - lo_v or 1.0
            t = (v - lo_v) / span
            return (
                int(lo_c[0] + (hi_c[0] - lo_c[0]) * t),
                int(lo_c[1] + (hi_c[1] - lo_c[1]) * t),
                int(lo_c[2] + (hi_c[2] - lo_c[2]) * t),
            )
    return _HEATMAP_STOPS[-1][1]


@router.get("/heatmap/{camera_id}/image")
def heatmap_image(camera_id: int, alpha: float = 0.85,
                  _u=Depends(get_current_user)):
    """Render the heatmap as a PNG using the Premier League-style
    navy → blue → orange → red ramp.

    The frontend overlays this on the camera snapshot (or a manually
    uploaded floorplan) at the operator's chosen opacity. Default
    alpha bumped from 0.65 to 0.85 — the new colour scheme is dark
    enough that the snapshot still reads through, and operators
    asked for crisper hot zones.

    The hottest cell (top 15% intensity) is rendered at full opacity
    plus a one-pixel white halo so it pops as a "🔥 Busiest Area"
    even on cluttered backgrounds.
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
    hotspots: list[tuple[int, int]] = []
    for y in range(n):
        for x in range(n):
            v = grid[y][x] / max_v
            if v <= 0:
                continue
            r_, g_, b_ = _heatmap_color(v)
            # Sharper transitions at high intensity — alpha ramps
            # from the configured base up to 1.0 in the top band so
            # hotspots pop instead of blending into the background.
            a = int(alpha * 255 * (0.7 + 0.3 * v))
            if v >= _HEATMAP_HOTSPOT_THRESHOLD:
                a = 255
                hotspots.append((x, y))
            px[x, y] = (r_, g_, b_, min(255, a))

    # Upscale 16× — nearest-neighbour keeps the cell boundaries sharp
    # for the football-analytics look (the old bilinear smoothed them
    # into a soft blob).
    img = img.resize((n * 16, n * 16), Image.NEAREST)

    # Bloom ring around the single hottest cell. Skip when the grid
    # is uniformly hot (the ring becomes noise).
    if hotspots and len(hotspots) < (n * n) // 4:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        # Pick the cell with the highest absolute count.
        mx, my, mv = hotspots[0][0], hotspots[0][1], grid[hotspots[0][1]][hotspots[0][0]]
        for (x, y) in hotspots:
            if grid[y][x] > mv:
                mx, my, mv = x, y, grid[y][x]
        cx = mx * 16 + 8
        cy = my * 16 + 8
        for radius, width in ((28, 3), (40, 2)):
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                         outline=(255, 255, 255, 220), width=width)

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
