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
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.analytics import recorder
from app.database import get_db
from app.deps import get_current_user
from app.utils.cache import cached_store_endpoint
from app.models import (
    Alert, Camera, DetectionEvent, MetricSnapshot, Store,
)
from app.schemas.store import MetricSnapshotOut

log = logging.getLogger(__name__)

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
                    since: datetime | None = None,
                    until: datetime | None = None,
                    db: Session = Depends(get_db), _u=Depends(get_current_user)):
    """Roll up the most-used KPIs for a single store.

    Window precedence:
      • Explicit `since`/`until` win (operator picked Yesterday /
        This week / Last 30 days / custom range).
      • Otherwise fall back to `days` (legacy 1/7/30 selector).
    Either way, occupancy_now / queue_now / shutter_now reflect the
    LATEST sample inside the chosen window — so Yesterday shows
    yesterday's last reading rather than the live one, and a multi-
    day window shows the period's most recent sample.
    """
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    now = datetime.now(timezone.utc)
    if since is not None:
        window_since = since
        window_until = until or now
    else:
        window_since = now - timedelta(days=days)
        window_until = now
    # Whether the operator asked for a historical window (anything
    # ending before "now-5min"). When True, "right now" KPIs are
    # replaced with the last sample inside the window instead of the
    # actual live reading — Yesterday should show yesterday's end-
    # of-day state, not the empty current minute.
    is_historical = (now - window_until).total_seconds() > 300

    # Shared aggregation helpers (app.utils.analytics_queries) — the
    # same SQL the chain dashboard uses, scoped to one store. Replaces
    # ~17 sequential queries (including computing each visitor sum
    # TWICE for the net figure) with 4 grouped round-trips and zero ORM
    # entity instantiation. "Now" KPIs use the 48h live lookback.
    from app.utils.analytics_queries import (
        fetch_alert_stats, fetch_latest_samples, fetch_metric_aggregates,
        fetch_visitor_counts)
    ids = [store_id]
    avg_map, sum_map = fetch_metric_aggregates(db, ids, window_since,
                                               window_until)
    last_map = fetch_latest_samples(
        db, ids, now=now,
        historical_window=((window_since, window_until)
                           if is_historical else None))
    vt_window, vt_today = fetch_visitor_counts(db, ids, window_since,
                                               window_until, date.today())
    _ab, _ = fetch_alert_stats(db, ids, window_since, window_until, now=now)
    alert_breakdown = _ab.get(store_id, {})
    visitors_in_window = vt_window.get(store_id, 0)
    unique_today = vt_today.get(store_id, 0)

    def _last(metric: str) -> float | None:
        return last_map.get((store_id, metric))

    def _avg(metric: str) -> float | None:
        return avg_map.get((store_id, metric))

    def _sum(metric: str) -> float | None:
        return sum_map.get((store_id, metric))


    return {
        "store_id": store.id,
        "store_name": store.name,
        "country": store.country,
        "as_of": now.isoformat(),
        "window_days": days,
        "window_since": window_since.isoformat(),
        "window_until": window_until.isoformat(),
        "is_historical": is_historical,
        "kpis": {
            "occupancy_now":        _last("occupancy"),
            "occupancy_avg":        _avg("occupancy"),
            "queue_length_now":     _last("queue_length"),
            "queue_length_avg":     _avg("queue_length"),
            "queue_wait_avg_sec":   _avg("queue_wait_seconds"),
            "staff_present_avg":    _avg("staff_present_pct"),
            # `unique_visitors_today` kept for tile compat. For
            # historical ranges the dashboard reads
            # `unique_visitors_in_window` instead so Yesterday /
            # Last 30 days shows the right number.
            "unique_visitors_today":     int(unique_today),
            "unique_visitors_in_window": int(visitors_in_window),
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

@router.get("/store/{store_id}/multi-camera")
@cached_store_endpoint("store-multi-camera", ttl=60)
def store_multi_camera(store_id: int,
                       db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    """Multi-camera store view — aggregated TODAY footfall for the whole
    store plus per-camera in/out/current-occupancy, preferring
    entry_exit-tagged cameras (all cameras when none are tagged).
    Reuses the analytics_queries helpers; cached 60s."""
    from zoneinfo import ZoneInfo

    from app.models import Zone
    from app.utils.analytics_queries import (
        fetch_camera_latest, fetch_camera_metric_sums,
    )

    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = (db.query(Camera).filter(Camera.store_id == store_id)
              .order_by(Camera.name).all())
    if not cams:
        return {"store_id": store_id, "store_name": store.name,
                "as_of": datetime.now(timezone.utc).isoformat(),
                "totals": {"in": 0, "out": 0, "net": 0, "occupancy": 0},
                "cameras": []}

    zone_rows = (db.query(Zone.camera_id, Zone.detection_types_json)
                   .filter(Zone.camera_id.in_([c.id for c in cams])).all())
    entrance_ids = {cid for cid, types in zone_rows
                    if "entry_exit" in (types or [])}
    selected = [c for c in cams if c.id in entrance_ids] or cams
    ids = [c.id for c in selected]

    now = datetime.now(timezone.utc)
    eat_midnight = (now.astimezone(ZoneInfo("Africa/Nairobi"))
                    .replace(hour=0, minute=0, second=0, microsecond=0))
    since = eat_midnight.astimezone(timezone.utc)

    sums = fetch_camera_metric_sums(db, ids, since, now)
    occ = fetch_camera_latest(db, ids, ("occupancy",), now=now)

    cameras = []
    t_in = t_out = t_occ = 0
    for c in selected:
        c_in = int(sums.get((c.id, "visitor_count_in")) or 0)
        c_out = int(sums.get((c.id, "visitor_count_out")) or 0)
        c_occ = int(occ.get((c.id, "occupancy")) or 0)
        t_in += c_in
        t_out += c_out
        t_occ += c_occ
        cameras.append({
            "camera_id": c.id, "name": c.name, "status": c.status,
            "entrance": c.id in entrance_ids,
            "in": c_in, "out": c_out, "occupancy": c_occ,
        })
    return {
        "store_id": store_id,
        "store_name": store.name,
        "as_of": now.isoformat(),
        "totals": {"in": t_in, "out": t_out,
                   "net": t_in - t_out, "occupancy": t_occ},
        "cameras": cameras,
    }


@router.get("/store/{store_id}/live")
@cached_store_endpoint("store-live", ttl=15)
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
              .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return float(v) if v is not None else 0.0

    def _max(metric: str, t0: datetime, t1: datetime) -> float:
        v = (db.query(func.max(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return float(v) if v is not None else 0.0

    def _avg(metric: str, t0: datetime, t1: datetime) -> float:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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

    def _count(metric: str, t0: datetime, t1: datetime) -> int:
        """COUNT(*) of rows for a metric in [t0,t1). Used by per-session
        metrics (e.g. checkout_dwell_seconds) where each row is one
        completed session and the natural KPI is "how many"."""
        v = (db.query(func.count(MetricSnapshot.id))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1)
               .scalar())
        return int(v or 0)

    def _kpi_count(metric: str) -> dict:
        c = _count(metric, active_since, active_until)
        p = _count(metric, prev_since,   prev_until)
        return {"value": c, "trend": _trend(c, p)}

    def _busiest_hour_eat(metric: str, t0: datetime, t1: datetime) -> int | None:
        """Hour (0..23) in the store's local timezone with the most
        rows for `metric` in [t0,t1). None when there's no data."""
        tz = store.timezone or "Africa/Nairobi"
        hour_expr = func.extract(
            "hour", func.timezone(tz, MetricSnapshot.period_start)
        )
        row = (db.query(hour_expr.label("h"),
                         func.count(MetricSnapshot.id).label("n"))
                 .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                         MetricSnapshot.metric_type == metric,
                         MetricSnapshot.period_start >= t0,
                         MetricSnapshot.period_start <  t1)
                 .group_by("h")
                 .order_by(func.count(MetricSnapshot.id).desc())
                 .first())
        return int(row.h) if (row and row.h is not None) else None

    def _latest_sum(metric: str) -> float:
        """Sum the latest sample of `metric` across the store's cameras.
        Use case: occupancy NOW = sum of each camera's most recent
        occupancy reading. (Without ReID, this can double-count people
        seen by two cameras simultaneously — see camera-aggregation doc.)"""
        # Subquery: latest period_start per camera_id for this metric.
        sub = (db.query(MetricSnapshot.camera_id,
                        func.max(MetricSnapshot.period_start).label("p"))
                 .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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

    # Unique visitors — STAFF EXCLUDED. Use the shared
    # daily_visitor_count helper so this tile reconciles exactly with
    # the hourly footfall chart's "Total today" line (single source
    # of truth — fallback chain unique_visitors → visitor_count_in →
    # occupancy).
    from app.utils.visitor_count import daily_visitor_count
    _curr = daily_visitor_count(db, store, since_utc=active_since,
                                until_utc=active_until, cam_ids=cam_ids)
    _prev = daily_visitor_count(db, store, since_utc=prev_since,
                                until_utc=prev_until, cam_ids=cam_ids)
    unique_curr = _curr.total
    unique_prev = _prev.total
    visitor_source = _curr.source
    visitor_source_label = _curr.source_label

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
                     .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                             MetricSnapshot.metric_type == "occupancy",
                             MetricSnapshot.period_start >= active_since,
                             MetricSnapshot.period_start <  active_until)
                     .group_by("hr").order_by("hr").all())
    hourly_footfall = [{"hour": int(h), "value": float(v or 0)} for h, v in hourly_rows]

    # Per-aisle dwell, top 3, in the active range.
    aisle_rows = (db.query(MetricSnapshot.zone_id,
                           func.avg(MetricSnapshot.value).label("d"))
                    .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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
            # Plain-English data source so the manager understands
            # whether this is true unique-track counting, entry/exit
            # line crossings, or an occupancy-peak estimate. Matches
            # the same field on /store/{id}/hourly.
            "data_source":       visitor_source,
            "data_source_label": visitor_source_label,
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
        # ---- Checkout dwell time (one row per completed transaction) -----
        "checkout_avg_seconds_today": {
            "value": round(_kpi_avg("checkout_dwell_seconds")["value"], 0),
            "trend": _kpi_avg("checkout_dwell_seconds")["trend"],
            "visible": capabilities["counter"],
        },
        "checkout_max_seconds_today": {
            "value": round(_kpi_max("checkout_dwell_seconds")["value"], 0),
            "trend": _kpi_max("checkout_dwell_seconds")["trend"],
            "visible": capabilities["counter"],
        },
        "checkout_count_today": {
            "value": _kpi_count("checkout_dwell_seconds")["value"],
            "trend": _kpi_count("checkout_dwell_seconds")["trend"],
            "visible": capabilities["counter"],
        },
        "checkout_busiest_hour_today": {
            "value": _busiest_hour_eat("checkout_dwell_seconds",
                                         active_since, active_until),
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


# ---- BI panels (May-2026 dashboard intelligence) -------------------
#
# Five focused endpoints powering the store analytics page's BI
# panels. Each returns ALREADY-COMPUTED insights (text strings) along
# with the raw numbers so the frontend doesn't have to recompute
# things like "peak hour today" on every render.
#
# All endpoints clamp to the store's business-hours session — after-
# hours noise never appears in any of them.

@router.get("/store/{store_id}/hourly")
@cached_store_endpoint("store-hourly", ttl=60)
def store_hourly(store_id: int,
                 since: datetime | None = None,
                 until: datetime | None = None,
                 db: Session = Depends(get_db),
                 _u=Depends(get_current_user)):
    """Hourly visitor breakdown for the given window (defaults to
    today's business-hours session) + same-window-yesterday
    comparison. Includes auto-generated insights.

    Top-level try/except: any unhandled failure (bad metric data,
    schema drift, transient DB blip) returns a clean empty hourly
    payload instead of a 500 so the dashboard chart still paints
    rather than showing a scary "rebuild the api container" error."""
    try:
        return _store_hourly_impl(store_id, since, until, db)
    except HTTPException:
        raise   # 404 etc. should still propagate
    except Exception as e:
        import logging as _l
        _l.getLogger(__name__).exception(
            "store_hourly failed for store=%s — returning empty payload: %s",
            store_id, e)
        payload = _empty_hourly()
        payload["store_id"] = store_id
        payload["insights"] = ["Visitor data not available yet."]
        return payload


def _store_hourly_impl(store_id: int,
                       since: datetime | None,
                       until: datetime | None,
                       db: Session) -> dict:
    from app.utils.business_hours import todays_session, _store_local_now
    from app.utils.visitor_count import daily_visitor_count, reconcile_warn
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return _empty_hourly()

    now = datetime.now(timezone.utc)
    local_now = _store_local_now(store, now)
    if since is not None:
        session_start = since
        session_end   = until or now
    else:
        session_start, session_end = todays_session(store, now)
    open_hr  = session_start.astimezone(local_now.tzinfo).hour
    close_hr = session_end.astimezone(local_now.tzinfo).hour or (open_hr + 12)
    current_hr = local_now.hour

    # Single source of truth — same helper that feeds the LIVE
    # dashboard's unique_visitors tile, so the chart total and the
    # daily summary tile always reconcile.
    today = daily_visitor_count(db, store, since_utc=session_start,
                                until_utc=session_end, cam_ids=cam_ids)
    yest_session_start = session_start - timedelta(days=1)
    yest_session_end   = session_end   - timedelta(days=1)
    yesterday = daily_visitor_count(db, store, since_utc=yest_session_start,
                                    until_utc=yest_session_end, cam_ids=cam_ids)

    by_hour = today.by_hour_eat
    yest_by_hour = yesterday.by_hour_eat

    def _intensity(v: float) -> str:
        if v >= 30: return "high"
        if v >= 10: return "medium"
        if v > 0:   return "low"
        return "empty"

    # Always paint the canonical 09:00-21:00 window so the chart has
    # 12 ticks even on a quiet day.
    chart_open = min(open_hr, 9)
    chart_close = max(close_hr, 21)
    hours = []
    hours_yesterday = []
    for hr in range(chart_open, max(chart_close, chart_open + 1)):
        v = by_hour.get(hr, 0)
        hours.append({
            "hour": hr,
            "label": f"{hr:02d}:00",
            "visitors": int(round(v)),
            "intensity": _intensity(v),
            "is_current": hr == current_hr,
        })
        hours_yesterday.append({
            "hour": hr,
            "visitors": int(round(yest_by_hour.get(hr, 0))),
        })

    # Chart-total = sum of the rendered bars. By construction this
    # equals `today.total` because both come from the same source,
    # but compute it independently and warn loudly if they ever
    # diverge — that's the canary for a future regression.
    today_total = sum(h["visitors"] for h in hours)
    reconcile_warn(store.name or f"store {store.id}",
                   hourly_sum=today_total, daily_total=today.total)
    today_total = today.total       # prefer the helper's total as truth

    yest_so_far = sum(v for h, v in yest_by_hour.items() if h <= current_hr)
    trend_delta_pct = None
    if yest_so_far > 0:
        trend_delta_pct = round((today_total - yest_so_far) / yest_so_far * 100, 1)

    # Peak / quiet — over hours that actually had data.
    populated = [(h, v) for h, v in by_hour.items() if v > 0]
    peak  = max(populated, key=lambda kv: kv[1]) if populated else None
    quiet = min(populated, key=lambda kv: kv[1]) if populated else None

    insights: list[str] = []
    if peak:
        insights.append(
            f"Peak hour today: {peak[0]:02d}:00-{peak[0]+1:02d}:00 "
            f"with {int(peak[1])} visitors")
    if quiet:
        insights.append(
            f"Quietest hour: {quiet[0]:02d}:00-{quiet[0]+1:02d}:00 "
            f"with {int(quiet[1])} visitors")
    restock_hour = None
    for hr in range(open_hr, min(open_hr + 3, close_hr)):
        if by_hour.get(hr, 0) <= 10:
            restock_hour = hr
            insights.append(
                f"Best time to restock: {hr:02d}:00-{hr+1:02d}:00 (lowest traffic)")
            break
    peak_v = peak[1] if peak else 0
    threshold = peak_v * 0.75
    busy_hours = sorted(h for h, v in by_hour.items() if v >= threshold and v > 0)
    if busy_hours:
        insights.append(
            f"Recommend extra staff at: {busy_hours[0]:02d}:00-"
            f"{busy_hours[-1]+1:02d}:00 (peak period)")
    if trend_delta_pct is not None:
        arrow = "▲" if trend_delta_pct > 0 else "▼" if trend_delta_pct < 0 else "▶"
        insights.append(
            f"{arrow} {abs(trend_delta_pct)}% vs same time yesterday")
    insights.append(today.source_label)

    return {
        "store_id": store_id,
        "open_hour":  open_hr,
        "close_hour": close_hr,
        "current_hour": current_hr,
        "metric_source": today.source,
        "data_source_label": today.source_label,
        "hours": hours,
        "hours_yesterday": hours_yesterday,
        "peak":  {"hour": peak[0],  "visitors": int(peak[1])}  if peak  else None,
        "quiet": {"hour": quiet[0], "visitors": int(quiet[1])} if quiet else None,
        "restock_hour": restock_hour,
        "busy_hour_range": [busy_hours[0], busy_hours[-1] + 1] if busy_hours else None,
        "today_total": int(today_total),
        "yesterday_so_far": int(yest_so_far),
        "trend_delta_pct": trend_delta_pct,
        "insights": insights,
    }


def _empty_hourly() -> dict:
    # Pad to the canonical 09:00–21:00 retail window so the chart
    # always paints — empty stores still show a 12-point flat line
    # instead of collapsing into "no data" text.
    hours = [{"hour": h, "label": f"{h:02d}:00", "visitors": 0,
              "intensity": "empty", "is_current": False}
             for h in range(9, 21)]
    hours_yesterday = [{"hour": h, "visitors": 0} for h in range(9, 21)]
    return {
        "store_id": None, "hours": hours, "hours_yesterday": hours_yesterday,
        "peak": None, "quiet": None,
        "open_hour": 9, "close_hour": 21, "current_hour": 0,
        "metric_source": None, "restock_hour": None,
        "busy_hour_range": None, "today_total": 0,
        "yesterday_so_far": 0, "trend_delta_pct": None,
        "insights": ["No cameras attached to this store yet."],
    }


@router.get("/store/{store_id}/behaviour")
def store_behaviour(store_id: int, since: datetime | None = None, until: datetime | None = None, db: Session = Depends(get_db),
                    _u=Depends(get_current_user)):
    """Customer journey + dwell aggregates for the journey-map panel.

    Empty payload + setup hint when no journey/zone data exists yet —
    the frontend renders an explanatory empty state in that case.
    """
    from app.models import CustomerJourney, Zone
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"has_journey_data": False,
                "setup_hint": "No cameras attached to this store yet."}

    now = datetime.now(timezone.utc)
    if since is not None:
        week_start = since
    else:
        week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Staff signatures from the past week — used to filter out staff
    # movement loops from the journey map (the user's complaint:
    # "Dwell time averages include staff movement which skews data").
    from app.models import StaffTrack as _StaffTrack
    staff_sigs = set(
        s for (s,) in
        db.query(_StaffTrack.track_signature)
          .filter(_StaffTrack.store_id == store_id,
                  _StaffTrack.day >= week_start.date(),
                  _StaffTrack.classified_as == "staff").all()
    )
    j_q = (db.query(CustomerJourney)
             .filter(CustomerJourney.store_id == store_id,
                     CustomerJourney.started_at >= week_start))
    if staff_sigs:
        j_q = j_q.filter(CustomerJourney.track_signature.notin_(staff_sigs))
    journeys = j_q.limit(1000).all()
    if not journeys:
        return {
            "has_journey_data": False,
            "setup_hint": (
                "Draw zones on your cameras (entrance, aisle, counter, exit) "
                "to unlock journey mapping. Each zone becomes a node in the "
                "Sankey flow diagram."
            ),
        }

    # Aggregate path counts. zone_sequence_json is a list of zone visits
    # in order. Each step can be:
    #   - a plain string ("Entrance")
    #   - a zone-id int (5)
    #   - a dict ({"zone_id": 5, "zone_name": "Entrance", "entered_at":
    #     "2026-05-14T..."}) — that's how the customer-journey detector
    #     writes it today.
    # We normalise to the human-readable zone NAME so the dashboard
    # renders pills like "Entrance" instead of the dict's Python repr.
    def _step_label(step) -> str:
        if isinstance(step, dict):
            # Prefer human-readable name, fall back to zone_id, then
            # the dict string only as a last resort.
            v = step.get("zone_name") or step.get("name")
            if v:
                return str(v)
            zid = step.get("zone_id") or step.get("id")
            if zid is not None:
                return f"Zone {zid}"
            return "Zone"
        return str(step)

    path_counts: dict[tuple[str, ...], int] = {}
    durations: list[int] = []
    zone_visits: dict[str, int] = {}
    completed_count = 0   # journeys whose last zone is "counter"-ish
    for j in journeys:
        seq = j.zone_sequence_json or []
        if not seq:
            continue
        compact: list[str] = []
        for step in seq:
            label = _step_label(step)
            if not compact or compact[-1] != label:
                compact.append(label)
        for z in compact:
            zone_visits[z] = zone_visits.get(z, 0) + 1
        path_counts[tuple(compact)] = path_counts.get(tuple(compact), 0) + 1
        if j.ended_at and j.started_at:
            durations.append(int((j.ended_at - j.started_at).total_seconds()))
        if any("counter" in (s or "").lower() or "checkout" in (s or "").lower() for s in compact):
            completed_count += 1

    total_journeys = len(journeys)
    avg_duration = (sum(durations) // len(durations)) if durations else 0
    completion_pct = round(completed_count / total_journeys * 100, 1) if total_journeys else 0

    common_path = max(path_counts.items(), key=lambda kv: kv[1])[0] if path_counts else ()
    # Most-skipped zone: zone with fewest visits relative to known set.
    known_zones = {z.name for z in db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()}
    skipped = []
    for zn in known_zones:
        visits = zone_visits.get(zn, 0)
        if total_journeys > 0:
            skipped.append((zn, visits / total_journeys))
    most_skipped = min(skipped, key=lambda kv: kv[1])[0] if skipped else None

    return {
        "has_journey_data": True,
        "total_journeys": total_journeys,
        "avg_journey_seconds": avg_duration,
        "completion_pct": completion_pct,
        "common_path": list(common_path),
        "most_skipped_zone": most_skipped,
        "zone_visit_counts": zone_visits,
        # Frontend renders this as Sankey-ish nodes + flows.
        "top_paths": [
            {"path": list(p), "count": c}
            for p, c in sorted(path_counts.items(), key=lambda kv: -kv[1])[:5]
        ],
    }


@router.get("/store/{store_id}/staff-timeline")
def store_staff_timeline(store_id: int, db: Session = Depends(get_db),
                         _u=Depends(get_current_user)):
    """Staffed/unstaffed periods for today, plus the current-status
    flag the dashboard's gauge uses."""
    from app.utils.business_hours import todays_session
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"has_data": False, "setup_hint": "No cameras attached."}

    now = datetime.now(timezone.utc)
    session_start, session_end = todays_session(store, now)
    # `staff_present_pct` is a [0,1] metric per camera per ~minute. We
    # treat the store as 'staffed' for a minute when ANY camera with a
    # counter zone reports >= 0.5 in that minute.
    rows = (
        db.query(MetricSnapshot.period_start, MetricSnapshot.value)
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "staff_present_pct",
                  MetricSnapshot.period_start >= session_start,
                  MetricSnapshot.period_start <  session_end)
          .order_by(MetricSnapshot.period_start).all()
    )
    if not rows:
        return {
            "has_data": False,
            "setup_hint": "Mark a 'staff counter' zone on one camera to enable this panel.",
            "today_pct": 0, "current_status": "unknown",
            "gaps": [], "insights": [],
        }

    # Bucket into per-minute states.
    minutes: dict[datetime, bool] = {}
    for ts, val in rows:
        m = ts.replace(second=0, microsecond=0)
        minutes[m] = max(float(val or 0) >= 0.5, minutes.get(m, False))

    staffed_minutes = sum(1 for v in minutes.values() if v)
    total_minutes = len(minutes) or 1
    today_pct = round(staffed_minutes / total_minutes * 100, 1)

    # Gaps: contiguous runs of unstaffed minutes >= 5min.
    sorted_minutes = sorted(minutes.items())
    gaps: list[dict] = []
    current_gap_start: datetime | None = None
    for m, ok in sorted_minutes:
        if not ok and current_gap_start is None:
            current_gap_start = m
        elif ok and current_gap_start is not None:
            dur = int((m - current_gap_start).total_seconds() // 60)
            if dur >= 5:
                gaps.append({
                    "start": current_gap_start.isoformat(),
                    "end":   m.isoformat(),
                    "duration_minutes": dur,
                })
            current_gap_start = None
    # Trailing open gap (still unstaffed right now).
    if current_gap_start is not None:
        dur = int((sorted_minutes[-1][0] - current_gap_start).total_seconds() // 60)
        if dur >= 5:
            gaps.append({
                "start": current_gap_start.isoformat(),
                "end":   None,
                "duration_minutes": dur,
                "ongoing": True,
            })

    current_status = "staffed" if sorted_minutes and sorted_minutes[-1][1] else "unstaffed"

    # ---- Uniform compliance (today) --------------------------------
    # `uniform_compliance_pct` is a [0,1] rolling metric written by the
    # UniformComplianceDetector. We average it over today's session and
    # count how many distinct violation alerts fired today.
    uni_rows = (
        db.query(func.avg(MetricSnapshot.value))
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "uniform_compliance_pct",
                  MetricSnapshot.period_start >= session_start,
                  MetricSnapshot.period_start <  session_end)
          .scalar()
    )
    uniform_pct = round(float(uni_rows) * 100, 1) if uni_rows is not None else None
    uniform_rag = None
    if uniform_pct is not None:
        uniform_rag = ("green" if uniform_pct > 95
                       else "amber" if uniform_pct >= 80 else "red")
    # Today's uniform violations with timestamps (for the list).
    viol_rows = (
        db.query(DetectionEvent.timestamp)
          .join(Alert, Alert.event_id == DetectionEvent.id)
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  DetectionEvent.detection_type == "uniform_compliance",
                  DetectionEvent.timestamp >= session_start)
          .order_by(DetectionEvent.timestamp.desc()).limit(20).all()
    )
    uniform_violations = [
        {"time": r[0].isoformat() if r[0] else None} for r in viol_rows
    ]

    # ---- Staff-zone (behind-counter) alerts today --------------------
    # Counts split by rule so the dashboard can show how often the
    # behind-counter area was breached today.
    sz_rows = (db.query(DetectionEvent.extra)
                 .join(Alert, Alert.event_id == DetectionEvent.id)
                 .filter(DetectionEvent.camera_id.in_(cam_ids),
                         DetectionEvent.detection_type == "staff_zone",
                         DetectionEvent.timestamp >= session_start)
                 .all())
    staff_zone_breaches = 0   # urgent: unidentified person / customer in staff area
    nametag_violations  = 0   # info: in uniform but no lanyard/tag
    for (extra,) in sz_rows:
        rule = (extra or {}).get("rule") if isinstance(extra, dict) else None
        if rule == "missing_nametag":
            nametag_violations += 1
        else:
            staff_zone_breaches += 1

    # ---- Staff identified today — by uniform vs by counter-dwell ----
    from app.models import StaffTrack
    today_date = datetime.now(timezone.utc).date()
    staff_rows = (db.query(StaffTrack.source, func.count(StaffTrack.id))
                    .filter(StaffTrack.store_id == store_id,
                            StaffTrack.day == today_date,
                            StaffTrack.classified_as == "staff")
                    .group_by(StaffTrack.source).all())
    staff_by_source = {(s or "zone"): int(n) for s, n in staff_rows}
    staff_by_uniform = staff_by_source.get("uniform", 0)
    staff_by_zone = staff_by_source.get("zone", 0)
    staff_identified_total = staff_by_uniform + staff_by_zone

    insights: list[str] = []
    if gaps:
        worst = max(gaps, key=lambda g: g["duration_minutes"])
        insights.append(
            f"Longest gap: {worst['duration_minutes']} min starting "
            f"{worst['start'][11:16]}"
        )
    ongoing = next((g for g in gaps if g.get("ongoing")), None)
    if ongoing:
        insights.insert(0, f"🔴 Counter unstaffed for {ongoing['duration_minutes']} min RIGHT NOW")
    if today_pct >= 90:
        insights.append(f"Staff presence on target ({today_pct}% ≥ 90%)")
    elif today_pct >= 70:
        insights.append(f"Staff presence below target ({today_pct}% — target ≥ 90%)")
    else:
        insights.append(f"Staff presence well below target ({today_pct}% — target ≥ 90%)")

    return {
        "has_data": True,
        "today_pct": today_pct,
        "current_status": current_status,
        "gaps": gaps,
        # Per-minute timeline for the horizontal bar; True = staffed.
        "timeline": [
            {"minute": m.isoformat(), "staffed": bool(v)}
            for m, v in sorted_minutes
        ],
        "insights": insights,
        # Uniform compliance (today). pct is null when the detector
        # isn't enabled / no staff scored yet.
        "uniform_compliance_pct": uniform_pct,
        "uniform_compliance_rag": uniform_rag,
        "uniform_violations_today": len(uniform_violations),
        "uniform_violations": uniform_violations,
        # Staff identified today, split by how they were recognised.
        "staff_identified_total": staff_identified_total,
        "staff_by_uniform": staff_by_uniform,
        "staff_by_zone": staff_by_zone,
        # Behind-counter compliance (staff_zone detector).
        "staff_zone_breaches_today": staff_zone_breaches,
        "nametag_violations_today":  nametag_violations,
        "compliance_verdict": (
            "Poor"           if staff_zone_breaches > 2 or (uniform_pct is not None and uniform_pct < 70)
            else "Needs attention" if staff_zone_breaches > 0 or nametag_violations > 2
                                       or (uniform_pct is not None and uniform_pct < 90)
            else "Good"
        ),
    }


@router.get("/store/{store_id}/zone-performance")
def store_zone_performance(store_id: int,
                            since: datetime | None = None,
                            until: datetime | None = None,
                            db: Session = Depends(get_db),
                            _u=Depends(get_current_user)):
    """Per-zone engagement + dwell ranking. Powers the visual-
    merchandising panel + the Zone Intelligence dashboard section."""
    from app.models import Zone
    from sqlalchemy import extract
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"zones": [], "setup_hint": "No cameras attached."}

    now = datetime.now(timezone.utc)
    if since is not None:
        week_start = since
    else:
        week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

    zones = db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()
    if not zones:
        return {
            "zones": [],
            "setup_hint": (
                "Draw zones on your cameras to unlock per-aisle metrics. "
                "Each zone you draw becomes a row here."
            ),
        }

    # Average dwell per zone.
    dwell_rows = dict(
        db.query(MetricSnapshot.zone_id, func.avg(MetricSnapshot.value))
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "dwell_seconds",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by(MetricSnapshot.zone_id).all()
    )
    # Engagement = visits to this zone / total store visits.
    visit_rows = dict(
        db.query(MetricSnapshot.zone_id, func.sum(MetricSnapshot.value))
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "zone_visit_count",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by(MetricSnapshot.zone_id).all()
    )
    total_visits = sum(float(v or 0) for v in visit_rows.values()) or 1.0

    # Peak hour per zone — busiest occupancy hour over the window.
    peak_rows = (db.query(MetricSnapshot.zone_id,
                          extract("hour", MetricSnapshot.period_start).label("hr"),
                          func.max(MetricSnapshot.value).label("v"))
                   .filter(((MetricSnapshot.store_id == store_id)
                            | MetricSnapshot.camera_id.in_(cam_ids)),
                           MetricSnapshot.metric_type == "occupancy",
                           MetricSnapshot.period_start >= week_start,
                           MetricSnapshot.period_start <  now)
                   .group_by(MetricSnapshot.zone_id, "hr").all())
    peak_by_zone: dict[int, tuple[int, float]] = {}
    for zid, hr, v in peak_rows:
        if zid is None or v is None:
            continue
        cur = peak_by_zone.get(int(zid))
        if cur is None or float(v) > cur[1]:
            peak_by_zone[int(zid)] = (int(hr), float(v))

    zone_rows: list[dict] = []
    for z in zones:
        tags = z.detection_types_json or []
        is_queue   = "queue"   in tags or "counter" in tags
        is_engage  = any(t in tags for t in ("aisle", "dwell", "window", "display", "shelf"))
        # Show queue/counter zones too — they get a different
        # recommendation ("open second checkout", not "well stocked").
        if not (is_engage or is_queue):
            continue
        dwell_s = float(dwell_rows.get(z.id) or 0)
        visits  = float(visit_rows.get(z.id) or 0)
        engagement_pct = round(visits / total_visits * 100, 1) if total_visits else 0
        peak_hr, _peak_v = peak_by_zone.get(z.id, (None, 0.0))
        # Engagement-score component (0..1) used by the layered
        # heatmap legend. Aisle/shelf zones score on dwell + visits;
        # queue/counter zones cap at 0 so they don't inflate the
        # engagement layer.
        if is_engage:
            engagement_score = round(
                min(1.0, 0.5 * (dwell_s / 120.0) + 0.5 * (engagement_pct / 100.0)),
                2,
            )
        else:
            engagement_score = 0.0
        if is_queue:
            recommendation = (f"⚠️ Bottleneck check — avg {int(dwell_s)}s wait. "
                              f"Open a second checkout point if sustained.")
            is_bottleneck = dwell_s > 180
        elif engagement_pct >= 70:
            recommendation = "High interest zone — ensure well stocked"
            is_bottleneck = False
        elif engagement_pct >= 40:
            recommendation = "Moderate interest — consider end-cap promotion"
            is_bottleneck = False
        else:
            recommendation = "Low engagement — move slow-selling items or add signage"
            is_bottleneck = False
        zone_rows.append({
            "zone_id": z.id,
            "name":    z.name,
            "tags":    tags,
            "engagement_pct": engagement_pct,
            "engagement_score": engagement_score,
            "avg_dwell_seconds": round(dwell_s, 1),
            "traffic_count":    int(visits),
            "peak_hour":        f"{peak_hr:02d}:00" if peak_hr is not None else None,
            "is_bottleneck":    is_bottleneck,
            "is_queue_zone":    is_queue,
            "recommendation":   recommendation,
            "rag": "green" if engagement_pct >= 70
                   else "amber" if engagement_pct >= 40
                   else "red",
        })
    zone_rows.sort(key=lambda r: -r["engagement_pct"])
    for i, row in enumerate(zone_rows, 1):
        row["rank"] = i

    insights: list[str] = []
    if zone_rows:
        top = zone_rows[0]
        insights.append(
            f"🏆 Top performer: {top['name']} — "
            f"{top['engagement_pct']}% engagement, "
            f"{int(top['avg_dwell_seconds']) // 60}m{int(top['avg_dwell_seconds']) % 60}s avg dwell. "
            f"Consider expanding display space."
        )
        weak = [r for r in zone_rows if r["engagement_pct"] < 40]
        if weak:
            w = weak[0]
            insights.append(
                f"⚠️ Underperformer: {w['name']} — only {w['engagement_pct']}% "
                f"of visitors stop here. Recommendation: move to higher-traffic zone "
                f"near entrance."
            )
        if zone_rows and zone_rows[0]["avg_dwell_seconds"] > 120:
            insights.append(
                "💡 High dwell zones drive engagement — keep them well-stocked "
                "and well-lit."
            )

    return {
        "zones": zone_rows,
        "insights": insights,
        "top_performer":  zone_rows[0]  if zone_rows else None,
        "underperformer": zone_rows[-1] if zone_rows else None,
    }


# ====================================================================
# Queue Intelligence — plain-English checkout performance report
# ====================================================================
# Uses the existing queue_length and queue_wait_seconds metrics that
# the QueueDetector writes per zone per minute. Everything else is
# derived: SLA breaches, hourly buckets, traffic-light bands, the
# executive summary and the three recommendations.

# Traffic-light bands. Wait time in seconds.
_QUEUE_GREEN_MAX = 120   # < 2 min  → green
_QUEUE_AMBER_MAX = 240   # 2-4 min  → amber, anything above → red


def _wait_status(seconds: float) -> str:
    if seconds < _QUEUE_GREEN_MAX:
        return "GREEN"
    if seconds < _QUEUE_AMBER_MAX:
        return "AMBER"
    return "RED"


def _fmt_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(round(float(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _trend_label(today_avg: float, prev_avg: float) -> str:
    if prev_avg <= 0 or today_avg <= 0:
        return "STABLE"
    delta = (today_avg - prev_avg) / prev_avg
    if delta > 0.1:
        return "WORSENING"
    if delta < -0.1:
        return "IMPROVING"
    return "STABLE"


@router.get("/store/{store_id}/queue-intelligence")
def store_queue_intelligence(store_id: int,
                             date: str | None = None,
                             since: datetime | None = None,
                             until: datetime | None = None,
                             db: Session = Depends(get_db),
                             _u=Depends(get_current_user)):
    """Plain-English queue performance report for a store + window.

    Window precedence:
      • explicit `since`/`until` (the DateRangePicker on the dashboard
        sends these) — used as-is
      • `date=YYYY-MM-DD` — that single calendar day (UTC)
      • neither — today (UTC midnight → now)

    Trend is computed against the immediately preceding window of the
    same length, so Yesterday vs day-before-yesterday, Last 7 vs the
    7 before that, etc."""
    from datetime import datetime as _dt, date as date_t, timedelta

    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")

    if since is not None:
        day_start = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        day_end   = (until if until and until.tzinfo else
                     (until.replace(tzinfo=timezone.utc) if until else _dt.now(timezone.utc)))
        target_date_label = day_start.date().isoformat()
        if day_start.date() != (day_end - timedelta(seconds=1)).date():
            target_date_label = (f"{day_start.date().isoformat()} → "
                                 f"{(day_end - timedelta(seconds=1)).date().isoformat()}")
    else:
        try:
            target_date = date_t.fromisoformat(date) if date else _dt.now(timezone.utc).date()
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        day_start = _dt(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        day_end   = day_start + timedelta(days=1)
        target_date_label = target_date.isoformat()
    window_len = day_end - day_start
    prev_start = day_start - window_len
    prev_end   = day_start

    cam_ids = [c.id for c in db.query(Camera).filter(Camera.store_id == store_id).all()]
    sla_seconds = int(getattr(store, "queue_sla_seconds", 180) or 180)
    sla_length  = int(getattr(store, "queue_sla_length", 6) or 6)

    if not cam_ids:
        return {
            "store_name": store.name, "date": target_date_label,
            "generated_at": _dt.now(timezone.utc).isoformat(),
            "summary": None, "sla": None, "status_breakdown": None,
            "hourly_breakdown": [],
            "peak_hour": None, "quietest_hour": None, "trend": "STABLE",
            "staffing_recommendation": None,
            "recommendations": ["Attach cameras to this store to start collecting queue data."],
            "executive_summary": "No queue data yet — attach a camera with a 'queue' zone to start.",
        }

    # Pull every queue_wait_seconds + queue_length sample for today.
    waits = (db.query(MetricSnapshot.period_start, MetricSnapshot.value)
               .filter(MetricSnapshot.camera_id.in_(cam_ids),
                       MetricSnapshot.metric_type == "queue_wait_seconds",
                       MetricSnapshot.period_start >= day_start,
                       MetricSnapshot.period_start <  day_end)
               .all())
    lengths = (db.query(MetricSnapshot.period_start, MetricSnapshot.value)
                 .filter(MetricSnapshot.camera_id.in_(cam_ids),
                         MetricSnapshot.metric_type == "queue_length",
                         MetricSnapshot.period_start >= day_start,
                         MetricSnapshot.period_start <  day_end)
                 .all())

    if not waits and not lengths:
        return {
            "store_name": store.name, "date": target_date_label,
            "generated_at": _dt.now(timezone.utc).isoformat(),
            "summary": None, "sla": None, "status_breakdown": None,
            "hourly_breakdown": [],
            "peak_hour": None, "quietest_hour": None, "trend": "STABLE",
            "staffing_recommendation": None,
            "recommendations": ["No queue activity recorded in this period.",
                                "Draw a 'queue' zone on the checkout camera if you haven't already."],
            "executive_summary": "No queue activity recorded in this period.",
        }

    wait_values = [float(v or 0) for _, v in waits]
    length_values = [float(v or 0) for _, v in lengths]
    avg_wait = sum(wait_values) / len(wait_values) if wait_values else 0.0
    peak_wait = max(wait_values) if wait_values else 0.0
    shortest_wait = min(wait_values) if wait_values else 0.0
    avg_length = sum(length_values) / len(length_values) if length_values else 0.0
    peak_length = max(length_values) if length_values else 0.0

    # SLA breach = wait above the store's target.
    breaches = sum(1 for v in wait_values if v > sla_seconds)
    breach_pct = round(breaches / len(wait_values) * 100, 1) if wait_values else 0.0
    if breach_pct == 0:
        sla_status = "OK"
    elif breach_pct < 10:
        sla_status = "AT RISK"
    else:
        sla_status = "BREACHING"

    # Traffic-light distribution of waits.
    n = max(1, len(wait_values))
    g = sum(1 for v in wait_values if v < _QUEUE_GREEN_MAX)
    a = sum(1 for v in wait_values if _QUEUE_GREEN_MAX <= v < _QUEUE_AMBER_MAX)
    r = sum(1 for v in wait_values if v >= _QUEUE_AMBER_MAX)

    # Hourly breakdown — group by hour of period_start.
    hour_buckets: dict[int, dict[str, list[float]]] = {}
    for ts, v in waits:
        hr = ts.hour
        b = hour_buckets.setdefault(hr, {"wait": [], "queue": []})
        b["wait"].append(float(v or 0))
    for ts, v in lengths:
        hr = ts.hour
        b = hour_buckets.setdefault(hr, {"wait": [], "queue": []})
        b["queue"].append(float(v or 0))
    hourly = []
    for hr in sorted(hour_buckets):
        b = hour_buckets[hr]
        aw = sum(b["wait"]) / len(b["wait"]) if b["wait"] else 0.0
        aq = sum(b["queue"]) / len(b["queue"]) if b["queue"] else 0.0
        hourly.append({
            "hour": f"{hr:02d}:00",
            "avg_wait": int(round(aw)),
            "avg_queue": round(aq, 1),
            "status": _wait_status(aw),
            "snapshots": len(b["wait"]) + len(b["queue"]),
        })

    peak_hour = max(hourly, key=lambda h: h["avg_wait"])["hour"] if hourly else None
    quietest_hour = (min((h for h in hourly if h["snapshots"] > 0),
                         key=lambda h: h["avg_wait"])["hour"]
                     if hourly else None)

    # Trend vs the prior same-length window — yesterday for a single
    # day, last-7 vs the 7 before for a weekly range, etc.
    prev_waits = (db.query(func.avg(MetricSnapshot.value))
                    .filter(MetricSnapshot.camera_id.in_(cam_ids),
                            MetricSnapshot.metric_type == "queue_wait_seconds",
                            MetricSnapshot.period_start >= prev_start,
                            MetricSnapshot.period_start <  prev_end)
                    .scalar() or 0)
    trend = _trend_label(avg_wait, float(prev_waits))

    # Staffing recommendation = the contiguous block of breaching hours
    # (avg wait above the SLA), if any.
    breaching_hours = [int(h["hour"][:2]) for h in hourly if h["avg_wait"] > sla_seconds]
    if breaching_hours:
        start_h, end_h = min(breaching_hours), max(breaching_hours) + 1
        staffing = f"{start_h:02d}:00–{end_h:02d}:00"
    else:
        staffing = None

    # Recommendations + executive summary — plain English.
    recs: list[str] = []
    if staffing:
        recs.append(
            f"Open a second till between {staffing} — queue peaks at "
            f"{int(peak_length)} customers during this period."
        )
    if peak_hour and peak_hour[:2].isdigit() and int(peak_hour[:2]) > 12:
        recs.append(
            "Rotate lunch breaks before 13:00 so all tills are open "
            "during the afternoon peak."
        )
    avg_mmss = _fmt_mmss(avg_wait)
    target_mmss = _fmt_mmss(sla_seconds)
    if avg_wait > sla_seconds:
        over = int(avg_wait - sla_seconds)
        recs.append(
            f"Average wait of {avg_mmss} is {over}s above the {target_mmss} "
            f"target. One extra staff member at peak times would help."
        )
    elif breach_pct > 0:
        recs.append(
            f"Average wait of {avg_mmss} is within the {target_mmss} target, "
            f"but {breach_pct}% of customers still waited longer than that."
        )
    else:
        recs.append(
            f"Average wait of {avg_mmss} is comfortably within the "
            f"{target_mmss} target — keep doing what you're doing."
        )
    while len(recs) < 3:
        recs.append("Keep an eye on the busiest hour and add a till there if waits creep up.")

    if breach_pct >= 10 and peak_hour:
        exec_sum = (
            f"{store.name} handled checkout well in quieter hours but saw "
            f"pressure build around {peak_hour}. {breach_pct}% of customers "
            f"waited more than {target_mmss} — above the SLA target. "
            f"Adding staff during {staffing or 'peak hours'} would bring waits "
            f"back in line."
        )
    elif breach_pct > 0:
        exec_sum = (
            f"{store.name} stayed close to target today. {breach_pct}% of "
            f"customers waited longer than {target_mmss}, mostly around "
            f"{peak_hour or 'peak'}. A small staffing tweak would clear it."
        )
    else:
        exec_sum = (
            f"{store.name} kept every customer within the {target_mmss} target "
            f"today. No staffing changes needed."
        )

    return {
        "store_name": store.name,
        "date": target_date_label,
        "generated_at": _dt.now(timezone.utc).isoformat(),
        "summary": {
            "avg_wait_seconds": int(round(avg_wait)),
            "peak_wait_seconds": int(round(peak_wait)),
            "shortest_wait_seconds": int(round(shortest_wait)),
            "avg_queue_length": round(avg_length, 1),
            "peak_queue_length": int(peak_length),
            "total_snapshots": len(waits) + len(lengths),
        },
        "sla": {
            "target_max_wait_seconds": sla_seconds,
            "target_max_queue_length": sla_length,
            "breaches": breaches,
            "breach_pct": breach_pct,
            "status": sla_status,
        },
        "status_breakdown": {
            "green_pct": round(g / n * 100, 1),
            "amber_pct": round(a / n * 100, 1),
            "red_pct":   round(r / n * 100, 1),
        },
        "hourly_breakdown": hourly,
        "peak_hour": peak_hour,
        "quietest_hour": quietest_hour,
        "trend": trend,
        "staffing_recommendation": staffing,
        "recommendations": recs[:3],
        "executive_summary": exec_sum,
    }


@router.get("/store/{store_id}/queue-snapshot")
def store_queue_snapshot(store_id: int,
                         db: Session = Depends(get_db),
                         _u=Depends(get_current_user)):
    """Live queue snapshot for the dashboard table. Reads the latest
    queue_length + queue_wait_seconds samples (last 2 min) and
    synthesises one row per customer in the queue, with descending
    wait times anchored on the measured average."""
    from datetime import datetime as _dt, timedelta

    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cam_ids = [c.id for c in db.query(Camera).filter(Camera.store_id == store_id).all()]
    if not cam_ids:
        return {"taken_at": _dt.now(timezone.utc).isoformat(),
                "active_counters": 0, "customers_in_queue": [],
                "summary": {}, "has_data": False}

    cutoff = _dt.now(timezone.utc) - timedelta(minutes=2)
    # Latest queue_length sample per (camera, zone).
    rows = (db.query(MetricSnapshot)
              .filter(MetricSnapshot.camera_id.in_(cam_ids),
                      MetricSnapshot.metric_type == "queue_length",
                      MetricSnapshot.period_start >= cutoff)
              .order_by(MetricSnapshot.period_start.desc()).all())
    latest_lengths: dict[tuple[int, int], MetricSnapshot] = {}
    for m in rows:
        k = (m.camera_id or 0, m.zone_id or 0)
        if k not in latest_lengths:
            latest_lengths[k] = m
    queue_count = int(sum(int(m.value or 0) for m in latest_lengths.values()))

    # Latest avg wait across the same zones.
    wait_rows = (db.query(MetricSnapshot.value)
                   .filter(MetricSnapshot.camera_id.in_(cam_ids),
                           MetricSnapshot.metric_type == "queue_wait_seconds",
                           MetricSnapshot.period_start >= cutoff)
                   .all())
    avg_wait = (sum(float(v or 0) for (v,) in wait_rows) / len(wait_rows)
                if wait_rows else 0.0)

    # Synthesise per-customer rows. We don't track individuals — only
    # aggregate counts — so we spread waits linearly from peak (the
    # front of the queue, ~1.6× avg) down to a short tail (~0.3× avg).
    customers = []
    if queue_count > 0 and avg_wait > 0:
        peak = avg_wait * 1.6
        tail = max(30.0, avg_wait * 0.3)
        for i in range(queue_count):
            t = i / max(1, queue_count - 1)
            wait = peak - (peak - tail) * t
            customers.append({
                "position": i + 1,
                "wait_seconds": int(round(wait)),
                "status": _wait_status(wait),
            })

    waits = [c["wait_seconds"] for c in customers]
    summary = {
        "avg_wait": _fmt_mmss(sum(waits) / len(waits)) if waits else "—",
        "peak_wait": _fmt_mmss(max(waits)) if waits else "—",
        "shortest_wait": _fmt_mmss(min(waits)) if waits else "—",
        "red_count":   sum(1 for c in customers if c["status"] == "RED"),
        "amber_count": sum(1 for c in customers if c["status"] == "AMBER"),
        "green_count": sum(1 for c in customers if c["status"] == "GREEN"),
        "sla_target": _fmt_mmss(int(getattr(store, "queue_sla_seconds", 180) or 180)),
    }
    return {
        "taken_at": _dt.now(timezone.utc).isoformat(),
        "active_counters": len(latest_lengths),
        "customers_in_queue": customers,
        "summary": summary,
        "has_data": bool(customers),
        # Honesty note for the dashboard footer.
        "note": "Per-customer waits are estimated from the measured queue length and average wait.",
    }


@router.get("/store/{store_id}/scorecard")
@cached_store_endpoint("store-scorecard", ttl=30)
def store_scorecard(store_id: int, since: datetime | None = None, until: datetime | None = None, db: Session = Depends(get_db),
                    _u=Depends(get_current_user)):
    """Daily performance scorecard — today vs target vs yesterday
    with RAG status per metric and an overall 0–100 score."""
    from app.utils.business_hours import todays_session
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"rows": [], "overall_score": None, "overall_rag": "slate"}

    now = datetime.now(timezone.utc)
    if since is not None:
        # Operator chose an explicit range — use it verbatim, and
        # the comparison window becomes the prior same-length slice.
        today_open  = since
        today_close = until or now
    else:
        today_open, today_close = todays_session(store, now)
    today_end = min(now, today_close)
    yest_open  = today_open  - timedelta(days=1)
    yest_close = today_close - timedelta(days=1)

    from app.models import VisitorTrack

    def _sum(metric: str, t0, t1) -> float:
        v = (db.query(func.sum(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1).scalar())
        return float(v or 0)

    def _avg(metric: str, t0, t1) -> float:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1).scalar())
        return float(v or 0)

    def _max(metric: str, t0, t1) -> float:
        v = (db.query(func.max(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1).scalar())
        return float(v or 0)

    # Visitors here = "estimated customers" — exclude tracks flagged
    # as staff by the periodic staff classifier (any track that spent
    # >10 minutes inside a counter zone today). See app/tasks/
    # staff_classifier.py for the rules.
    from app.models import StaffTrack
    def _unique(t0, t1) -> int:
        return (db.query(func.count(VisitorTrack.id))
                  .outerjoin(StaffTrack,
                             (StaffTrack.store_id == store_id)
                             & (StaffTrack.day == VisitorTrack.day)
                             & (StaffTrack.track_signature == VisitorTrack.track_signature))
                  .filter(((VisitorTrack.store_id == store_id) | VisitorTrack.camera_id.in_(cam_ids)),
                          VisitorTrack.first_seen >= t0,
                          VisitorTrack.first_seen <  t1,
                          (StaffTrack.classified_as.is_(None))
                          | (StaffTrack.classified_as != "staff"))
                  .scalar() or 0)

    def _alerts(t0, t1) -> int:
        return (db.query(func.count(Alert.id))
                  .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                  .filter(DetectionEvent.camera_id.in_(cam_ids),
                          Alert.created_at >= t0,
                          Alert.created_at <  t1).scalar() or 0)

    def _delta(today: float, yesterday: float, *,
               higher_is_better: bool = True) -> dict:
        """Compute the today-vs-yesterday change and RAG bucket.

        Targets removed in the May-2026 redesign — store managers
        found the placeholder targets (5000 visitors, 700 occupancy)
        actively confusing. The signal that matters is "are we
        having a worse day than yesterday?", and yesterday's value
        is the only honest benchmark we have.

        RAG rules:
          green : today >= yesterday              (matching or up)
          amber : today is 10–30% below yesterday (mild concern)
          red   : today is >30% below yesterday   (big drop)
          slate : no yesterday baseline to compare to

        For lower-is-better metrics (queue wait, incidents) we
        invert: green when today <= yesterday, etc.
        """
        if yesterday is None or yesterday == 0:
            # No baseline. Treat any positive value as informational
            # (slate); zeros stay slate to avoid false alarms.
            return {"direction": "flat", "delta_pct": None, "rag": "slate"}
        # Signed pct: +25 means today is 25% above yesterday.
        pct = (today - yesterday) / yesterday * 100
        # For lower-is-better, "up" means "got worse".
        bad_direction = (pct > 0) if not higher_is_better else (pct < 0)
        abs_pct = abs(pct)
        if not bad_direction:
            rag = "green"
        elif abs_pct <= 10:
            rag = "green"      # tolerance band
        elif abs_pct <= 30:
            rag = "amber"
        else:
            rag = "red"
        direction = "up" if pct > 0.5 else "down" if pct < -0.5 else "flat"
        return {"direction": direction, "delta_pct": round(pct, 1), "rag": rag}

    today_visitors  = _unique(today_open, today_end)
    yest_visitors   = _unique(yest_open,  yest_close)
    today_peak_occ  = _max("occupancy", today_open, today_end)
    yest_peak_occ   = _max("occupancy", yest_open,  yest_close)
    today_queue_avg = _avg("queue_wait_seconds", today_open, today_end)
    yest_queue_avg  = _avg("queue_wait_seconds", yest_open,  yest_close)
    today_staff_pct = _avg("staff_present_pct",  today_open, today_end)
    yest_staff_pct  = _avg("staff_present_pct",  yest_open,  yest_close)
    today_alerts    = _alerts(today_open, today_end)
    yest_alerts     = _alerts(yest_open,  yest_close)
    today_dwell     = _avg("dwell_seconds", today_open, today_end)
    yest_dwell      = _avg("dwell_seconds", yest_open,  yest_close)

    def _row(metric: str, today: float, yesterday: float,
             unit: str = "", *, higher_is_better: bool = True) -> dict:
        d = _delta(today, yesterday, higher_is_better=higher_is_better)
        return {
            "metric": metric,
            "today": today,
            "yesterday": yesterday,
            "unit": unit,
            "higher_is_better": higher_is_better,
            **d,
        }

    rows = [
        _row("Visitors",       round(today_visitors),         round(yest_visitors)),
        _row("Peak Occupancy", round(today_peak_occ),         round(yest_peak_occ)),
        _row("Avg Queue Wait", round(today_queue_avg / 60, 1),
                                round(yest_queue_avg / 60, 1), "m",
             higher_is_better=False),
        _row("Staff Presence", round(today_staff_pct * 100),
                                round(yest_staff_pct * 100),  "%"),
        _row("Incidents",      today_alerts,                  yest_alerts, "",
             higher_is_better=False),
        _row("Avg Dwell Time", round(today_dwell / 60, 1),
                                round(yest_dwell / 60, 1),    "m"),
    ]

    # Headline: where does the store sit vs yesterday overall? Count
    # rows by RAG bucket and pick the majority signal.
    greens = sum(1 for r in rows if r["rag"] == "green")
    reds   = sum(1 for r in rows if r["rag"] == "red")
    ambers = sum(1 for r in rows if r["rag"] == "amber")
    if reds > greens:
        overall_direction = "worse"
        overall_rag       = "red"
        headline = "▼ Worse than yesterday overall"
    elif greens > reds + ambers:
        overall_direction = "better"
        overall_rag       = "green"
        headline = "▲ Better than yesterday overall"
    else:
        overall_direction = "same"
        overall_rag       = "amber"
        headline = "→ Roughly the same as yesterday"

    return {
        "rows": rows,
        "overall_direction": overall_direction,   # 'better' | 'same' | 'worse'
        "overall_rag":       overall_rag,
        "headline":          headline,
        "as_of":             now.isoformat(),
    }


@router.get("/store/{store_id}/visitor-intelligence")
def store_visitor_intelligence(store_id: int, db: Session = Depends(get_db),
                                _u=Depends(get_current_user)):
    """3-layer visitor intelligence (May-2026 redesign).

    Returns:
      • total_persons             — every VisitorTrack seen today
      • staff_tracks              — count classified 'staff' by the
                                    spatial heuristic (counter dwell
                                    > 10 min)
      • estimated_customers       — total_persons − staff_tracks,
                                    de-duped across cameras by
                                    track-time clustering (Layer 2)
      • staff_signatures          — array of the staff `track_signature`
                                    values (debug + audit visibility)
      • avg_dwell_seconds         — average dwell across CUSTOMER
                                    tracks only, excluding counter
                                    zone dwell (staff zone)
      • zones_visited_avg         — average distinct zones per
                                    customer journey (max == total
                                    aisle/dwell zones in the store)
      • visit_quality_score       — avg_dwell_minutes ×
                                    (zones_visited_avg / total_zones)
                                    × 100 — bounded 0-100ish
      • confidence                — 'high' when an entrance zone is
                                    configured (we know where to
                                    count from), else 'estimated'
      • explanation               — plain-English string for the
                                    dashboard footnote

    All numbers are for TODAY only. Future-upgrade comments live in
    app/tasks/staff_classifier.py.
    """
    from app.models import CustomerJourney, StaffTrack, VisitorTrack, Zone
    from app.utils.business_hours import todays_session

    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return _empty_visitor_intelligence("no cameras attached")

    now = datetime.now(timezone.utc)
    today_open, _ = todays_session(store, now)
    today = today_open.date()

    # Total person-tracks today.
    tracks_today = (
        db.query(VisitorTrack)
          .filter(((VisitorTrack.store_id == store_id) | VisitorTrack.camera_id.in_(cam_ids)),
                  VisitorTrack.first_seen >= today_open,
                  VisitorTrack.first_seen <  now)
          .all()
    )
    total_persons = len(tracks_today)

    # Staff signatures classified for today.
    staff_rows = (
        db.query(StaffTrack)
          .filter(StaffTrack.store_id == store_id,
                  StaffTrack.day == today,
                  StaffTrack.classified_as == "staff")
          .all()
    )
    staff_sigs = {s.track_signature for s in staff_rows}
    customer_tracks = [t for t in tracks_today if t.track_signature not in staff_sigs]

    # Layer 2 — cross-camera deduplication. The same shopper appears
    # under different track_signatures on different cameras (we don't
    # have ReID), but the time windows overlap. Greedily group
    # customer tracks whose [first_seen, last_seen] windows fall
    # within 30 minutes of an existing cluster's window. Each cluster
    # = one estimated customer.
    DEDUPE_GAP_SEC = 30 * 60
    clusters: list[tuple[datetime, datetime]] = []
    for t in sorted(customer_tracks, key=lambda x: x.first_seen):
        merged = False
        for i, (cstart, cend) in enumerate(clusters):
            if t.first_seen <= cend + timedelta(seconds=DEDUPE_GAP_SEC):
                clusters[i] = (min(cstart, t.first_seen),
                                max(cend,   t.last_seen or t.first_seen))
                merged = True
                break
        if not merged:
            clusters.append((t.first_seen, t.last_seen or t.first_seen))
    estimated_customers = len(clusters)

    # Customer dwell (Layer 3): from CustomerJourney rows owned by
    # non-staff signatures, sum non-counter zone dwell time.
    store_zones = db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()
    counter_zone_ids = {
        z.id for z in store_zones
        if "counter" in (z.detection_types_json or [])
    }
    total_zones = len(store_zones)

    customer_journeys = (
        db.query(CustomerJourney)
          .filter(CustomerJourney.store_id == store_id,
                  CustomerJourney.day == today)
          .all()
    )
    customer_journeys = [j for j in customer_journeys if j.track_signature not in staff_sigs]

    dwell_sum = 0.0
    dwell_count = 0
    zones_per_journey: list[int] = []
    for j in customer_journeys:
        distinct_zones: set[int] = set()
        for step in (j.zone_sequence_json or []):
            if not isinstance(step, dict):
                continue
            zid = step.get("zone_id")
            if zid is None:
                continue
            distinct_zones.add(zid)
            if zid in counter_zone_ids:
                continue   # staff area — drop from customer dwell math
            enter = _parse_iso_safe(step.get("entered_at"))
            exit_ = _parse_iso_safe(step.get("exited_at"))
            if enter and exit_:
                dur = (exit_ - enter).total_seconds()
                if dur > 0:
                    dwell_sum += dur
                    dwell_count += 1
        if distinct_zones:
            zones_per_journey.append(len(distinct_zones))

    avg_dwell_seconds = (dwell_sum / dwell_count) if dwell_count else 0.0
    zones_visited_avg = (sum(zones_per_journey) / len(zones_per_journey)) if zones_per_journey else 0.0

    # Visit quality: dwell-minutes × zone-coverage, scaled to a 0-100
    # ballpark. Below 25 = low, above 75 = excellent.
    coverage = (zones_visited_avg / total_zones) if total_zones else 0
    visit_quality_score = round((avg_dwell_seconds / 60.0) * coverage * 100, 1)

    # Confidence: 'high' if an entry/exit zone exists (we can locate
    # the start of the journey), else 'estimated'.
    entrance_present = any(
        ("entry_exit" in (z.detection_types_json or []))
        for z in store_zones
    )
    confidence = "high" if entrance_present else "estimated"

    return {
        "store_id": store_id,
        "store_name": store.name,
        "as_of": now.isoformat(),
        "day": today.isoformat(),
        "total_persons":       total_persons,
        "staff_tracks":        len(staff_sigs),
        "estimated_customers": estimated_customers,
        "staff_signatures":    sorted(staff_sigs),
        "avg_dwell_seconds":   round(avg_dwell_seconds, 1),
        "zones_visited_avg":   round(zones_visited_avg, 2),
        "total_zones":         total_zones,
        "visit_quality_score": visit_quality_score,
        "confidence":          confidence,
        "explanation": (
            f"Today: {total_persons} total persons detected, "
            f"{len(staff_sigs)} staff tracks identified, "
            f"~{estimated_customers} estimated customers. "
            f"Average customer visited {round(zones_visited_avg, 1)} zones "
            f"and spent {int(avg_dwell_seconds // 60)} min "
            f"{int(avg_dwell_seconds % 60)} sec."
        ),
        "footnote": (
            "Staff identified by counter presence >10 min "
            f"({'entrance zone configured — high confidence' if entrance_present else 'no entrance zone — estimated count'})"
        ),
    }


def _empty_visitor_intelligence(reason: str) -> dict:
    return {
        "total_persons": 0, "staff_tracks": 0, "estimated_customers": 0,
        "staff_signatures": [], "avg_dwell_seconds": 0,
        "zones_visited_avg": 0, "total_zones": 0,
        "visit_quality_score": 0, "confidence": "none",
        "explanation": f"No data yet — {reason}.",
        "footnote": "",
    }


def _parse_iso_safe(s):
    if not s:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# ---- Week summary + detector activity (May-2026 redesign) ---------

@router.get("/store/{store_id}/week-summary")
@cached_store_endpoint("store-week-summary", ttl=300)
def store_week_summary(store_id: int, since: datetime | None = None, until: datetime | None = None, db: Session = Depends(get_db),
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
    from app.utils.business_hours import todays_session
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
    if since is not None:
        week_start = since
    else:
        week_start = (now - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    today_session_open, _ = todays_session(store, now)

    # --- 7-day footfall: prefer visitor_count_in; fall back to the
    #     max occupancy of the day if entry_exit isn't instrumented.
    visitor_in_rows = (
        db.query(extract("doy", MetricSnapshot.period_start).label("doy"),
                 func.sum(MetricSnapshot.value).label("v"))
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "visitor_count_in",
                  MetricSnapshot.period_start >= week_start,
                  MetricSnapshot.period_start <  now)
          .group_by("doy").all()
    )
    visitor_by_doy = {int(r.doy): float(r.v or 0) for r in visitor_in_rows}

    occ_max_rows = (
        db.query(extract("doy", MetricSnapshot.period_start).label("doy"),
                 func.max(MetricSnapshot.value).label("v"))
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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
          .filter(((MetricSnapshot.store_id == store_id) | MetricSnapshot.camera_id.in_(cam_ids)),
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
def store_heatmaps(store_id: int, window: str | None = None,
                   db: Session = Depends(get_db),
                   _u=Depends(get_current_user)):
    """One row per camera in the store with its heatmap thumb URL plus
    the top hotspot location (highest-count cell). Frontend grids them.

    `window` (hour|day|week) propagates through to the per-camera
    thumb URL so the heatmap grid page can switch all tiles at once
    instead of one-camera-at-a-time.
    """
    import json
    import redis
    from app.config import settings as _settings
    if not db.get(Store, store_id):
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    r = redis.from_url(_settings.redis_url)
    out = []
    # Heatmap thumbs the dashboard renders inherit the window.
    win = _resolve_window(window)
    for c in cams:
        raw = r.get(_heatmap_redis_key(c.id, win))
        if not raw:
            # Legacy un-suffixed key fallback so cameras whose worker
            # hasn't been restarted yet still show a thumb.
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
                log.warning("heatmap hotspot computation failed camera=%s",
                            c.id, exc_info=True)
        out.append({
            "camera_id": c.id,
            "camera_name": c.name,
            "status": c.status,
            "heatmap_url": f"/api/analytics/heatmap/{c.id}/image?alpha=0.85&window={win}",
            "window": win,
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

# Allowed values for the `window` query parameter on heatmap reads.
# Maps each to the Redis key suffix the inference worker publishes
# under (see HeatmapDetector._publish).
# Window names accepted by the heatmap endpoints. Mirrors
# HeatmapDetector.WINDOWS. "hour" is kept as a synonym for "3h" so
# any cached frontend bundle from before the rename still works.
_HEATMAP_WINDOWS = {"3h", "day", "week"}
_HEATMAP_WINDOW_ALIASES = {"hour": "3h"}


def _resolve_window(window: str | None) -> str:
    """Normalise `?window=` to a canonical bucket name.
    `hour` is accepted as an alias for `3h` so any cached frontend
    bundle from before the rename keeps working."""
    if not window:
        return "day"
    w = _HEATMAP_WINDOW_ALIASES.get(window, window)
    return w if w in _HEATMAP_WINDOWS else "day"


def _heatmap_redis_key(camera_id: int, window: str | None) -> str:
    """Pick the per-window Redis key. None / 'day' / unknown all map
    to the day-window key so legacy callers keep working."""
    return f"vg:heatmap:{_resolve_window(window)}:{camera_id}"


def _heatmap_payload(camera_id: int, window: str | None) -> dict | None:
    """Fetch + JSON-decode the per-window heatmap payload, with a
    transparent fallback to the legacy un-suffixed key for cameras
    that haven't been re-published since the upgrade."""
    import json
    import redis
    from app.config import settings
    r = redis.from_url(settings.redis_url)
    raw = r.get(_heatmap_redis_key(camera_id, window))
    if not raw:
        # Backwards-compat: older workers wrote only the un-suffixed
        # key. If the new key isn't there yet, fall back so the
        # dashboard doesn't go blank during a rolling deploy.
        raw = r.get(f"vg:heatmap:{camera_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _heatmap_layer_payload(camera_id: int, window: str | None,
                            layer: str) -> dict | None:
    """Read a behavioural heatmap layer (engagement / congestion /
    paths) from Redis. Layer keys mirror HeatmapDetector._publish."""
    import json
    import redis
    from app.config import settings
    win = _resolve_window(window)
    key = f"vg:heatmap:{layer}:{win}:{camera_id}"
    r = redis.from_url(settings.redis_url)
    raw = r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.get("/heatmap/{camera_id}")
def heatmap_grid(camera_id: int, window: str | None = None,
                 db: Session = Depends(get_db),
                 _u=Depends(get_current_user)):
    """Return the latest heatmap grid for the requested time window.

    `window` is '3h' (last 3-hour bucket — resets at 00, 03, 06, 09,
    12, 15, 18, 21) | 'day' (default — since local midnight) | 'week'
    (since Monday 00:00). The inference worker maintains separate
    accumulators for each so picking a window doesn't require any
    backfill. `hour` is accepted as a synonym for `3h` for
    backwards-compat with older cached frontend bundles.
    """
    payload = _heatmap_payload(camera_id, window)
    if payload is None:
        raise HTTPException(404, "heatmap not available — is heatmap detection enabled?")
    payload["window"] = _resolve_window(window)
    payload["period_label"] = _period_label_for(payload["window"])
    # Busiest-hour label — independent of the spatial grid; pulled from
    # MetricSnapshot so it reflects actual today / week occupancy rather
    # than the cumulative heatmap intensity.
    payload["peak_hour"], payload["peak_label"] = _peak_hour_for(db, camera_id, payload["window"])
    return payload


def _peak_hour_for(db: Session, camera_id: int, window: str) -> tuple[int | None, str | None]:
    """Hour-of-day with the highest occupancy for this camera in the
    selected window. Returns (hour, "HH:00-HH+1:00") or (None, None)
    if no data."""
    from datetime import datetime, timedelta
    from sqlalchemy import extract
    now = datetime.now()
    if window == "3h":
        since = now.replace(hour=(now.hour // 3) * 3, minute=0, second=0, microsecond=0)
    elif window == "week":
        since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)

    row = (db.query(extract("hour", MetricSnapshot.period_start).label("hr"),
                    func.max(MetricSnapshot.value).label("v"))
             .filter(MetricSnapshot.camera_id == camera_id,
                     MetricSnapshot.metric_type == "occupancy",
                     MetricSnapshot.period_start >= since)
             .group_by("hr")
             .order_by(func.max(MetricSnapshot.value).desc())
             .first())
    if not row or not row[1]:
        return None, None
    hr = int(row[0])
    return hr, f"{hr:02d}:00–{hr + 1:02d}:00"


# ---- Engagement / congestion / paths layers ------------------------

@router.get("/heatmap/{camera_id}/engagement")
def heatmap_engagement(camera_id: int, window: str | None = None,
                       _u=Depends(get_current_user)):
    """Customer-engagement layer — dwell-seconds (×1.5 for repeat
    visits) on shelf / aisle / window / display zones only. Staff
    tracks excluded. Returns the same payload shape as
    /heatmap/{id} so the frontend can use the same renderer."""
    payload = _heatmap_layer_payload(camera_id, window, "engagement")
    if payload is None:
        raise HTTPException(404, "engagement heatmap not available yet")
    payload["window"] = _resolve_window(window)
    payload["period_label"] = _period_label_for(payload["window"])
    return payload


@router.get("/heatmap/{camera_id}/traffic")
def heatmap_traffic(camera_id: int, window: str | None = None,
                    db: Session = Depends(get_db),
                    _u=Depends(get_current_user)):
    """Foot-traffic layer — alias of /heatmap/{id} for symmetry with
    /engagement and /congestion. Same payload."""
    return heatmap_grid(camera_id, window, db, _u)


@router.get("/heatmap/{camera_id}/congestion")
def heatmap_congestion(camera_id: int, window: str | None = None,
                       _u=Depends(get_current_user)):
    """Congestion layer — dwell on queue / counter zones (waiting,
    not interest). Use for bottleneck alerts."""
    payload = _heatmap_layer_payload(camera_id, window, "congestion")
    if payload is None:
        raise HTTPException(404, "congestion heatmap not available yet")
    payload["window"] = _resolve_window(window)
    payload["period_label"] = _period_label_for(payload["window"])
    return payload


@router.get("/heatmap/{camera_id}/paths")
def heatmap_paths(camera_id: int, window: str | None = None,
                  _u=Depends(get_current_user)):
    """Movement-edge list — `[{from: [gy, gx], to: [gy, gx], count}]`
    sorted desc by count, capped to 200 strongest edges. Drives the
    customer-path arrows on the heatmap."""
    payload = _heatmap_layer_payload(camera_id, window, "paths")
    if payload is None:
        raise HTTPException(404, "paths heatmap not available yet")
    payload["window"] = _resolve_window(window)
    payload["period_label"] = _period_label_for(payload["window"])
    return payload


@router.get("/heatmap/{camera_id}/replay")
def heatmap_replay(camera_id: int,
                   day: str | None = None,
                   layer: str = "traffic",
                   db: Session = Depends(get_db),
                   _u=Depends(get_current_user)):
    """Hourly snapshots of one heatmap layer across a day. Returns
    24 entries (one per hour, possibly nulls), each {hour, grid_data,
    peak_value, captured_at}. Default day = today. Used by the
    HeatmapPage replay slider."""
    from datetime import datetime, date as date_t, timezone
    from app.models import HeatmapGridSnapshot

    if layer not in ("traffic", "engagement", "congestion", "paths"):
        raise HTTPException(400, f"unknown layer: {layer}")
    try:
        d = date_t.fromisoformat(day) if day else date_t.today()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    day_end   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)

    rows = (db.query(HeatmapGridSnapshot)
              .filter(HeatmapGridSnapshot.camera_id == camera_id,
                      HeatmapGridSnapshot.layer == layer,
                      HeatmapGridSnapshot.captured_at >= day_start,
                      HeatmapGridSnapshot.captured_at <= day_end)
              .order_by(HeatmapGridSnapshot.captured_at).all())

    by_hour: dict[int, dict] = {}
    for snap in rows:
        by_hour[int(snap.hour)] = {
            "hour": int(snap.hour),
            "grid_data": snap.grid_data,
            "peak_value": float(snap.peak_value or 0),
            "captured_at": snap.captured_at.isoformat(),
        }
    frames = [by_hour.get(h) or {"hour": h, "grid_data": None,
                                  "peak_value": 0, "captured_at": None}
              for h in range(24)]
    return {"camera_id": camera_id, "day": d.isoformat(),
            "layer": layer, "frames": frames}


def _period_label_for(window: str) -> str:
    """Human-readable label for the current bucket so the frontend
    can show "Last 3h: 14:00–17:00" without re-deriving the
    boundary itself."""
    from datetime import datetime, timedelta
    now = datetime.now()
    if window == "3h":
        start_hour = (now.hour // 3) * 3
        end_hour   = start_hour + 3
        return f"{start_hour:02d}:00–{end_hour:02d}:00"
    if window == "week":
        # ISO week → Monday of the current week.
        monday = now - timedelta(days=now.weekday())
        return f"Week of {monday.strftime('%b %d')}"
    return f"Today, since 00:00 ({now.strftime('%b %d')})"


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
                  window: str | None = None,
                  layer: str = "traffic",
                  _u=Depends(get_current_user)):
    """Render the heatmap as a PNG using the Premier League-style
    navy → blue → orange → red ramp.

    `layer` selects which grid to render:
      traffic     — foot traffic density (default — existing behaviour)
      engagement  — customer engagement score (aisle/shelf only)
      congestion  — checkout / queue dwell (bottleneck heatmap)

    `window` selects the time aggregator the inference worker keeps:
      3h   — rolling 3-hour bucket
      day  — today's grid since local 00:00 (default)
      week — this iso-week, reset Monday 00:00

    The frontend overlays the PNG on the camera snapshot (or a manually
    uploaded floorplan) at the operator's chosen opacity. The hottest
    cell (top 15% intensity) gets full opacity plus a white bloom
    ring so it pops as a "🔥 Busiest Area" on cluttered backgrounds.
    """
    import io
    from fastapi.responses import StreamingResponse

    if layer in {"engagement", "congestion"}:
        payload = _heatmap_layer_payload(camera_id, window, layer)
    else:
        payload = _heatmap_payload(camera_id, window)
    if payload is None:
        raise HTTPException(404, f"{layer} heatmap not available yet")
    grid = payload.get("grid") or []
    n    = payload.get("size") or len(grid) or 32
    if not grid:
        raise HTTPException(404, f"{layer} heatmap empty")

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


@router.get("/store/{store_id}/journeys")
def store_reid_journeys(store_id: int,
                        date: str = Query("today"),
                        limit: int = Query(200, le=1000),
                        db: Session = Depends(get_db),
                        _u=Depends(get_current_user)):
    """Cross-camera customer journeys for one store-day (Sprint 2.2 Re-ID).

    Distinct from /analytics/journeys/{store_id}, which reconstructs
    single-camera zone sequences. This endpoint stitches a shopper's path
    ACROSS cameras using the Re-ID `global_person_id` stamped on
    visitor_tracks, returning one entry per physical person with the
    cameras they visited (in arrival order) and their in-store dwell.

    Returns empty journeys when Re-ID is disabled / not yet populated —
    the dashboard then simply hides the journey panel.
    """
    from collections import Counter, OrderedDict
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    from app.models import Camera, StaffTrack, VisitorTrack

    if date in ("today", "", None):
        target_day = _date.today()
    else:
        try:
            target_day = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="date must be ISO YYYY-MM-DD or 'today'")

    store = db.get(Store, store_id)
    tz_name = (getattr(store, "timezone", None) or "Africa/Nairobi") if store else "Africa/Nairobi"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Africa/Nairobi")

    def _hhmm(dt):
        if dt is None:
            return None
        try:
            return dt.astimezone(tz).strftime("%H:%M")
        except Exception:
            return dt.strftime("%H:%M")

    rows = (db.query(VisitorTrack)
              .filter(VisitorTrack.store_id == store_id,
                      VisitorTrack.day == target_day,
                      VisitorTrack.global_person_id.isnot(None))
              .all())

    # Camera id -> display name.
    cam_ids = {r.camera_id for r in rows if r.camera_id is not None}
    cam_names: dict[int, str] = {}
    if cam_ids:
        for c in db.query(Camera).filter(Camera.id.in_(cam_ids)).all():
            cam_names[c.id] = c.name

    # track_signatures classified as staff today → mark those journeys.
    staff_sigs = {
        sig for (sig,) in
        db.query(StaffTrack.track_signature)
          .filter(StaffTrack.store_id == store_id,
                  StaffTrack.day == target_day,
                  StaffTrack.classified_as == "staff").all()
    }

    agg: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        gid = r.global_person_id
        a = agg.get(gid)
        if a is None:
            a = {"global_id": gid, "_cams": [], "_seen": set(),
                 "first_seen": r.first_seen, "last_seen": r.last_seen,
                 "is_staff": False}
            agg[gid] = a
        cname = (cam_names.get(r.camera_id)
                 or (f"Camera {r.camera_id}" if r.camera_id else "Unknown"))
        if cname not in a["_seen"]:
            a["_seen"].add(cname)
            a["_cams"].append((r.first_seen, cname))
        if r.first_seen and (a["first_seen"] is None or r.first_seen < a["first_seen"]):
            a["first_seen"] = r.first_seen
        if r.last_seen and (a["last_seen"] is None or r.last_seen > a["last_seen"]):
            a["last_seen"] = r.last_seen
        if r.track_signature in staff_sigs:
            a["is_staff"] = True

    journeys = []
    path_counter: Counter = Counter()
    for a in agg.values():
        cams = [c for _, c in sorted(a["_cams"], key=lambda t: (t[0] or 0))]
        dwell_min = 0
        if a["first_seen"] and a["last_seen"]:
            dwell_min = max(0, int((a["last_seen"] - a["first_seen"]).total_seconds() // 60))
        journeys.append({
            "global_id": a["global_id"],
            "cameras_visited": cams,
            "first_seen": _hhmm(a["first_seen"]),
            "last_seen": _hhmm(a["last_seen"]),
            "total_dwell_minutes": dwell_min,
            "is_staff": a["is_staff"],
        })
        if not a["is_staff"] and len(cams) >= 2:
            path_counter[tuple(cams)] += 1

    journeys.sort(key=lambda j: (j["first_seen"] or ""))
    top_paths = [{"path": list(seq), "count": cnt}
                 for seq, cnt in path_counter.most_common(10)]

    unique_people = len(agg)
    unique_customers = sum(1 for a in agg.values() if not a["is_staff"])
    camera_appearances = len(rows)

    return {
        "store_id": store_id,
        "date": target_day.isoformat(),
        "unique_people": unique_people,           # all global identities
        "unique_customers": unique_customers,     # staff excluded
        "camera_appearances": camera_appearances,  # raw visitor_track rows
        "top_paths": top_paths,
        "journeys": journeys[:limit],
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


# ---- Training-data export (shop-open pipeline) ---------------------
#
# Emits one row per shop_open_close DetectionEvent in the last
# `days` (default 30), with the row's training_label and the
# 07:00–09:30 EAT occupancy snapshots for that store on that day.
# Used to retrain the opening pipeline against the false positives
# that the occupancy-metric fallback identified in alerting.py.

@router.get("/training/shop-open")
def shop_open_training_export(
    days: int = Query(30, le=180),
    store_id: Optional[int] = None,
    label: Optional[str] = Query(None, description="filter by training_label, e.g. 'false_positive'"),
    db: Session = Depends(get_db),
    _u=Depends(get_current_user),
):
    from zoneinfo import ZoneInfo
    from app.models import MetricSnapshot
    eat = ZoneInfo("Africa/Nairobi")
    since_utc = datetime.now(timezone.utc) - timedelta(days=days)

    q = (db.query(DetectionEvent, Camera.store_id, Camera.name, Store.name)
           .join(Camera, Camera.id == DetectionEvent.camera_id)
           .outerjoin(Store, Store.id == Camera.store_id)
           .filter(DetectionEvent.detection_type == "shop_open_close",
                   DetectionEvent.timestamp >= since_utc))
    if store_id is not None:
        q = q.filter(Camera.store_id == store_id)
    q = q.order_by(DetectionEvent.timestamp.desc()).limit(2000)

    events_out: list[dict] = []
    for ev, sid, cam_name, store_name in q.all():
        ex = ev.extra or {}
        if label is not None and ex.get("training_label") != label:
            continue
        ts = ev.timestamp
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_eat = ts.astimezone(eat) if ts else None

        # Occupancy samples for this store on the event's morning
        # window (07:00–09:30 EAT). Skipped when we have no store_id
        # — older detection_events from before the store-backfill
        # might not have one.
        samples_payload: list[dict] = []
        if ts_eat is not None and sid is not None:
            day = ts_eat.date()
            ws_eat = datetime(day.year, day.month, day.day, 7,  0, tzinfo=eat)
            we_eat = datetime(day.year, day.month, day.day, 9, 30, tzinfo=eat)
            cam_ids_for_store = [
                cid for (cid,) in db.query(Camera.id)
                                     .filter(Camera.store_id == sid).all()
            ]
            if cam_ids_for_store:
                samples = (db.query(MetricSnapshot.period_start,
                                    MetricSnapshot.value,
                                    MetricSnapshot.camera_id,
                                    MetricSnapshot.metric_type)
                             .filter(MetricSnapshot.camera_id.in_(cam_ids_for_store),
                                     MetricSnapshot.metric_type.in_(
                                         ("occupancy", "queue_length", "passersby")),
                                     MetricSnapshot.value > 0,
                                     MetricSnapshot.period_start >= ws_eat.astimezone(timezone.utc),
                                     MetricSnapshot.period_start <= we_eat.astimezone(timezone.utc))
                             .order_by(MetricSnapshot.period_start.asc())
                             .limit(500).all())
                for ps, val, cid, mt in samples:
                    if ps.tzinfo is None:
                        ps = ps.replace(tzinfo=timezone.utc)
                    samples_payload.append({
                        "period_start_eat": ps.astimezone(eat).isoformat(),
                        "value":            float(val),
                        "camera_id":        cid,
                        "metric_type":      mt,
                    })

        events_out.append({
            "event_id":       ev.id,
            "camera_id":      ev.camera_id,
            "camera_name":    cam_name,
            "store_id":       sid,
            "store_name":     store_name,
            "rule":           ex.get("rule"),
            "timestamp_eat":  ts_eat.isoformat() if ts_eat else None,
            "training_label": ex.get("training_label"),
            "evidence":       ex.get("evidence"),
            "extra":          ex,
            "occupancy_samples": samples_payload,
            "occupancy_sample_count": len(samples_payload),
            "occupancy_peak": (max((s["value"] for s in samples_payload), default=0.0)
                               if samples_payload else 0.0),
        })

    return {
        "since_eat":  (datetime.now(timezone.utc) - timedelta(days=days))
                       .astimezone(eat).isoformat(),
        "count":      len(events_out),
        "events":     events_out,
    }


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
@cached_store_endpoint("chain-multi", ttl=60)
def multi_store(db: Session = Depends(get_db), _u=Depends(get_current_user),
                days: int = 7,
                since: datetime | None = None,
                until: datetime | None = None):
    """Chain-wide aggregate — powers /chain. Adds the May-2026 fields:

      • cameras_online / cameras_total per store
      • is_open per store (via business hours)
      • rag_status: 'red' | 'amber' | 'green' summarising overall health
      • needs_attention[] — the subset of stores with rag != 'green',
        each with the precise reason ("3 cameras offline",
        "high-priority alert in last hour", …) so head-office can
        triage at a glance.
      • best_store_today — highest visitor count today
      • totals.alerts_critical — high-priority subset of alerts_total
    """
    from collections import defaultdict
    from app.utils.business_hours import is_store_open
    from app.stream.frame_buffer import FrameBuffer

    stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
    fb = FrameBuffer()
    now = datetime.now(timezone.utc)
    rows = []

    # KPI thresholds for the RAG light. Tunable in one place so we
    # don't scatter magic numbers through the loop body.
    STAFF_MIN_PCT = 0.7        # below 70% → amber
    CRITICAL_TYPES = {"intrusion", "fight", "weapon",
                      "weapon_brandished", "shrinkage", "fire"}

    # ── BATCHED CHAIN AGGREGATION (perf rewrite) ──────────────────────
    # Previously this endpoint called store_dashboard() PER STORE
    # (~17 queries each) plus a per-store camera query and a per-camera
    # Redis GET — ~500 SQL queries + ~100 Redis RTTs per request for 28
    # stores. Everything below computes the SAME per-store row shape
    # from ~10 chain-wide GROUP BY queries + one Redis MGET.
    if since is not None:
        window_since = since
        window_until = until or now
    else:
        window_since = now - timedelta(days=days)
        window_until = now
    is_historical = (now - window_until).total_seconds() > 300

    import time as _time
    _perf: dict[str, float] = {}
    _t0 = _time.perf_counter()

    def _mark(stage: str) -> None:
        nonlocal _t0
        _now = _time.perf_counter()
        _perf[stage] = round((_now - _t0) * 1000.0, 1)
        _t0 = _now

    store_ids = [s.id for s in stores]

    # One camera pull for the whole chain (id + store only).
    all_cams = (db.query(Camera.id, Camera.store_id)
                  .filter(Camera.store_id.in_(store_ids)).all()
                if store_ids else [])
    cams_by_store: dict[int, list[int]] = defaultdict(list)
    for cid, sid in all_cams:
        cams_by_store[sid].append(cid)

    # One MGET for camera liveness across the chain.
    health = fb.health_many([cid for cid, _sid in all_cams])
    online_by_store: dict[int, int] = defaultdict(int)
    for cid, sid in all_cams:
        h = health.get(cid) or {}
        lfa = h.get("last_frame_at")
        if lfa and (now.timestamp() - float(lfa)) < 30:
            online_by_store[sid] += 1
    _mark("meta_redis")

    from app.utils.analytics_queries import (
        fetch_alert_stats, fetch_latest_samples, fetch_metric_aggregates,
        fetch_visitor_counts)

    # One pass computes avg AND sum per (store, metric); a second,
    # 48h-bounded (live) window query gets the "now" samples. Shared
    # with /dashboard/store/{id} via app.utils.analytics_queries.
    avg_map, sum_map = fetch_metric_aggregates(db, store_ids,
                                               window_since, window_until)
    last_map = fetch_latest_samples(
        db, store_ids, now=now,
        historical_window=((window_since, window_until)
                           if is_historical else None))
    _mark("latest_samples")

    vt_window, vt_today = fetch_visitor_counts(db, store_ids, window_since,
                                                window_until, date.today())
    ab_map, rc_map = fetch_alert_stats(
        db, store_ids, window_since, window_until,
        now=now, critical_types=tuple(CRITICAL_TYPES))
    _mark("grouped_aggregates")

    for s in stores:
        sid = s.id
        vin = sum_map.get((sid, "visitor_count_in"))
        vout = sum_map.get((sid, "visitor_count_out"))
        # Row shape identical to store_dashboard()'s return — the
        # frontend contract is unchanged, only the query plan is.
        row = {
            "store_id": s.id,
            "store_name": s.name,
            "country": s.country,
            "as_of": now.isoformat(),
            "window_days": days,
            "window_since": window_since.isoformat(),
            "window_until": window_until.isoformat(),
            "is_historical": is_historical,
            "kpis": {
                "occupancy_now":        last_map.get((sid, "occupancy")),
                "occupancy_avg":        avg_map.get((sid, "occupancy")),
                "queue_length_now":     last_map.get((sid, "queue_length")),
                "queue_length_avg":     avg_map.get((sid, "queue_length")),
                "queue_wait_avg_sec":   avg_map.get((sid, "queue_wait_seconds")),
                "staff_present_avg":    avg_map.get((sid, "staff_present_pct")),
                "unique_visitors_today":     int(vt_today.get(sid, 0)),
                "unique_visitors_in_window": int(vt_window.get(sid, 0)),
                "visitors_in_window":   vin,
                "visitors_out_window":  vout,
                "visitors_net_window":  (vin or 0) - (vout or 0),
                "passersby_avg":        avg_map.get((sid, "passersby")),
                "stop_rate_avg":        avg_map.get((sid, "stop_rate")),
                "dwell_seconds_avg":    avg_map.get((sid, "dwell_seconds")),
                "shutter_open_now":     last_map.get((sid, "shutter_open")),
            },
            "alerts_breakdown": dict(ab_map.get(sid, {})),
        }

        cam_total = len(cams_by_store.get(sid, []))
        cam_online = online_by_store.get(sid, 0)
        open_now = is_store_open(s)
        recent_critical = int(rc_map.get(sid, 0))

        staff_pct = row["kpis"].get("staff_present_avg")

        # RAG rules — first matching rule wins.
        #   red   : open AND (no cameras live OR critical alert in last hour)
        #   amber : open AND (staff < threshold OR some cameras offline)
        #   green : everything fine, or store is closed (closed != failing)
        reasons: list[str] = []
        rag = "green"
        if open_now and cam_online == 0:
            rag = "red"
            reasons.append(
                f"{cam_total} cameras attached but none streaming"
                if cam_total else "no cameras attached"
            )
        if open_now and recent_critical > 0:
            rag = "red"
            reasons.append(
                f"{recent_critical} critical alert{'s' if recent_critical != 1 else ''} in the last hour"
            )
        if rag != "red":
            if open_now and staff_pct is not None and staff_pct < STAFF_MIN_PCT:
                rag = "amber"
                reasons.append(f"staff presence {round(staff_pct*100)}% (target ≥ {round(STAFF_MIN_PCT*100)}%)")
            if open_now and cam_total > 0 and cam_online < cam_total:
                rag = "amber" if rag == "green" else rag
                reasons.append(f"{cam_total - cam_online} of {cam_total} cameras offline")

        row.update({
            "cameras_online": cam_online,
            "cameras_total":  cam_total,
            "is_open":        open_now,
            "rag_status":     rag,
            "rag_reasons":    reasons,
            "recent_critical_alerts": int(recent_critical),
        })
        rows.append(row)

    # Per-store visitor totals — pick the window-aware count when the
    # operator chose a historical range, fall back to "today" for the
    # default. Without this, the chain hero tile read 0 for Yesterday
    # / This week / Last 30 days because every store's "today" was 0
    # for those windows.
    def _store_visitors(r: dict) -> int:
        if since is not None:
            return int(r["kpis"].get("unique_visitors_in_window") or 0)
        return int(r["kpis"].get("unique_visitors_today") or 0)

    visitors_total = sum(_store_visitors(r) for r in rows)
    alerts_total = sum(sum(r["alerts_breakdown"].values()) for r in rows)
    alerts_critical = sum(r["recent_critical_alerts"] for r in rows)

    best_store = None
    if rows:
        best = max(rows, key=_store_visitors)
        if _store_visitors(best) > 0:
            best_store = {
                "store_id":   best["store_id"],
                "store_name": best["store_name"],
                "visitors":   _store_visitors(best),
            }

    needs_attention = [r for r in rows if r["rag_status"] in ("red", "amber")]
    needs_attention.sort(key=lambda r: (
        0 if r["rag_status"] == "red" else 1,
        -r["recent_critical_alerts"],
        -_store_visitors(r),
    ))

    _mark("assemble")
    log.info("chain-multi cold path ms: %s (stores=%d, historical=%s)",
             _perf, len(rows), is_historical)
    totals = {
        "unique_visitors_today": visitors_total,
        "unique_visitors_in_window": visitors_total,
        "stores":          len(rows),
        "alerts_total":    alerts_total,
        "alerts_critical": alerts_critical,
        "stores_open":     sum(1 for r in rows if r["is_open"]),
        "stores_attention": len(needs_attention),
    }
    return {
        "stores": rows,
        "totals": totals,
        "best_store_today": best_store,
        "needs_attention": [
            {
                "store_id":   r["store_id"],
                "store_name": r["store_name"],
                "country":    r["country"],
                "rag_status": r["rag_status"],
                "rag_reasons": r["rag_reasons"],
                "cameras_online": r["cameras_online"],
                "cameras_total":  r["cameras_total"],
            } for r in needs_attention
        ],
        "as_of": now.isoformat(),
    }


# =====================================================================
# AI ANALYTICS (May-2026 Priority-2 endpoints)
# =====================================================================
# Anomaly detection, predictive staffing, store health score, loss-
# prevention intelligence, behaviour trends, CV quality score. Each is
# self-contained and uses only existing tables — no external services.


@router.get("/store/{store_id}/anomalies")
@cached_store_endpoint("store-anomalies", ttl=300)
def store_anomalies(store_id: int, db: Session = Depends(get_db),
                    _u=Depends(get_current_user)):
    """Compare today's hourly footfall against the 30-day rolling
    average for that (weekday, hour) bucket. Anything >2σ away counts
    as an anomaly — surfaced on the dashboard as "Unusual" callouts.

    Uses MetricSnapshot rows of type `visitor_count_in` falling back
    to `occupancy`. Per-hour by weekday because Saturdays look
    nothing like Tuesdays in retail."""
    from sqlalchemy import extract
    from statistics import mean, stdev
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"anomalies": [], "today": [], "note": "no cameras attached"}

    now = datetime.now(timezone.utc)
    today_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = today_open - timedelta(days=30)
    metric = "visitor_count_in"

    # Pull 30 days of per-day-per-hour summed visitors.
    hist_rows = (
        db.query(
            extract("dow",  MetricSnapshot.period_start).label("dow"),
            extract("hour", MetricSnapshot.period_start).label("h"),
            extract("doy",  MetricSnapshot.period_start).label("d"),
            func.sum(MetricSnapshot.value).label("v"),
        )
        .filter(((MetricSnapshot.store_id == store_id)
                 | MetricSnapshot.camera_id.in_(cam_ids)),
                MetricSnapshot.metric_type == metric,
                MetricSnapshot.period_start >= history_start,
                MetricSnapshot.period_start < today_open)
        .group_by("dow", "h", "d")
        .all()
    )
    if not hist_rows:
        metric = "occupancy"
        hist_rows = (
            db.query(
                extract("dow", MetricSnapshot.period_start).label("dow"),
                extract("hour", MetricSnapshot.period_start).label("h"),
                extract("doy", MetricSnapshot.period_start).label("d"),
                func.max(MetricSnapshot.value).label("v"),
            )
            .filter(((MetricSnapshot.store_id == store_id)
                     | MetricSnapshot.camera_id.in_(cam_ids)),
                    MetricSnapshot.metric_type == metric,
                    MetricSnapshot.period_start >= history_start,
                    MetricSnapshot.period_start < today_open)
            .group_by("dow", "h", "d")
            .all()
        )

    # Group by (weekday, hour) → list of values across the 30-day window.
    buckets: dict[tuple[int, int], list[float]] = {}
    for r in hist_rows:
        buckets.setdefault((int(r.dow), int(r.h)), []).append(float(r.v or 0))

    # Today's per-hour values.
    today_rows = (
        db.query(
            extract("hour", MetricSnapshot.period_start).label("h"),
            func.sum(MetricSnapshot.value).label("v")
                if metric == "visitor_count_in" else
                func.max(MetricSnapshot.value).label("v"),
        )
        .filter(((MetricSnapshot.store_id == store_id)
                 | MetricSnapshot.camera_id.in_(cam_ids)),
                MetricSnapshot.metric_type == metric,
                MetricSnapshot.period_start >= today_open,
                MetricSnapshot.period_start < now)
        .group_by("h").all()
    )
    today_by_hour = {int(r.h): float(r.v or 0) for r in today_rows}

    today_dow = today_open.weekday()        # Mon=0..Sun=6 in python
    # Postgres dow: 0=Sunday..6=Saturday — convert.
    pg_dow = (today_dow + 1) % 7

    anomalies: list[dict] = []
    for hr, val in today_by_hour.items():
        history = buckets.get((pg_dow, hr), [])
        if len(history) < 4:        # not enough history to flag
            continue
        mu = mean(history)
        sigma = stdev(history) if len(history) > 1 else 0
        if sigma == 0:
            continue
        z = (val - mu) / sigma
        if abs(z) >= 2:
            direction = "above" if z > 0 else "below"
            multiplier = round(val / mu, 1) if mu > 0 else None
            anomalies.append({
                "hour":    hr,
                "label":   f"{hr:02d}:00",
                "value":   round(val, 1),
                "expected": round(mu, 1),
                "stdev":   round(sigma, 1),
                "z_score": round(z, 1),
                "direction": direction,
                "headline": (
                    f"⚠️ Unusual: {store.name} had {multiplier}× normal visitors at {hr:02d}:00"
                    if direction == "above" and multiplier and multiplier >= 2
                    else
                    f"⚠️ {store.name} had zero customers at {hr:02d}:00 (normally busy)"
                    if direction == "below" and val == 0
                    else
                    f"⚠️ {store.name} at {hr:02d}:00 — {direction} normal ({round(val)} vs avg {round(mu)})"
                ),
            })

    return {
        "store_id": store_id,
        "store_name": store.name,
        "anomalies": anomalies,
        "metric_source": metric,
        "note": ("Comparing today vs the last 30 same-weekday slots."
                 if anomalies else "No anomalies detected today."),
    }


@router.get("/store/{store_id}/staffing-prediction")
def store_staffing_prediction(store_id: int, db: Session = Depends(get_db),
                              _u=Depends(get_current_user)):
    """Simple linear regression of footfall by (weekday, hour) over
    the past 28 days. Output: recommended staff headcount per
    (weekday, hour) for the upcoming week, using the spec's
    1-staff-per-15-visitors target ratio.
    """
    from sqlalchemy import extract
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"recommendations": [], "note": "no cameras attached"}

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=28)
    rows = (
        db.query(
            extract("dow",  MetricSnapshot.period_start).label("dow"),
            extract("hour", MetricSnapshot.period_start).label("h"),
            func.sum(MetricSnapshot.value).label("v"),
        )
        .filter(((MetricSnapshot.store_id == store_id)
                 | MetricSnapshot.camera_id.in_(cam_ids)),
                MetricSnapshot.metric_type == "visitor_count_in",
                MetricSnapshot.period_start >= start,
                MetricSnapshot.period_start < now)
        .group_by("dow", "h").all()
    )
    if not rows:
        return {"recommendations": [],
                "note": "Not enough visitor data yet — need at least a week of entry/exit traffic."}

    VISITORS_PER_STAFF = 15
    dow_names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
    recs: list[dict] = []
    # Aggregate over the 4 weeks of history — the avg per (dow, hour)
    # bucket is the simplest "regression" that doesn't need numpy.
    bucket_avg: dict[tuple[int, int], float] = {}
    bucket_count: dict[tuple[int, int], int] = {}
    for r in rows:
        key = (int(r.dow), int(r.h))
        bucket_avg[key] = bucket_avg.get(key, 0.0) + float(r.v or 0)
        bucket_count[key] = bucket_count.get(key, 0) + 1
    for key, total in bucket_avg.items():
        avg = total / max(1, bucket_count[key])
        if avg < 1:
            continue
        dow, hr = key
        recommended = max(1, int(round(avg / VISITORS_PER_STAFF)))
        recs.append({
            "weekday":          dow_names.get(dow, str(dow)),
            "weekday_idx":      dow,
            "hour":             hr,
            "label":            f"{hr:02d}:00",
            "expected_visitors":int(round(avg)),
            "recommended_staff":recommended,
            "headline":         (f"Recommend {recommended} staff "
                                 f"{dow_names.get(dow, '?')} {hr:02d}:00-{hr+1:02d}:00"),
        })
    # Sort by weekday then hour for a clean weekly grid.
    recs.sort(key=lambda r: (r["weekday_idx"], r["hour"]))
    return {
        "store_id": store_id,
        "store_name": store.name,
        "recommendations": recs,
        "target_ratio": f"1 staff per {VISITORS_PER_STAFF} visitors",
    }


@router.get("/store/{store_id}/peak-prediction")
def store_peak_prediction(store_id: int, db: Session = Depends(get_db),
                          _u=Depends(get_current_user)):
    """Predict today's busy window based on the last 7 days of
    visitor traffic. Returns the hour-of-day that historically peaks
    on this weekday for this store, plus a 1-hour window centred on
    that hour.

    Output: {expected_peak_hour, label, mean_visitors_in_peak,
             based_on_days, headline}.
    """
    from sqlalchemy import extract
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"expected_peak_hour": None, "label": None,
                "headline": "No cameras attached yet"}

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    rows = (
        db.query(extract("hour", MetricSnapshot.period_start).label("h"),
                 func.sum(MetricSnapshot.value).label("v"))
          .filter(((MetricSnapshot.store_id == store_id)
                   | MetricSnapshot.camera_id.in_(cam_ids)),
                  MetricSnapshot.metric_type == "visitor_count_in",
                  MetricSnapshot.period_start >= start,
                  MetricSnapshot.period_start <  now)
          .group_by("h").all()
    )
    # Fallback to occupancy if no entry/exit data yet — same approach
    # as the hourly endpoint so the prediction works on day-1 cameras.
    if not rows or not any(float(r.v or 0) for r in rows):
        rows = (
            db.query(extract("hour", MetricSnapshot.period_start).label("h"),
                     func.max(MetricSnapshot.value).label("v"))
              .filter(((MetricSnapshot.store_id == store_id)
                       | MetricSnapshot.camera_id.in_(cam_ids)),
                      MetricSnapshot.metric_type == "occupancy",
                      MetricSnapshot.period_start >= start,
                      MetricSnapshot.period_start <  now)
              .group_by("h").all()
        )
    if not rows:
        return {"expected_peak_hour": None, "label": None, "based_on_days": 0,
                "headline": "Not enough history yet — need at least a few days of traffic."}

    by_hour = {int(r.h): float(r.v or 0) for r in rows}
    populated = [(h, v) for h, v in by_hour.items() if v > 0]
    if not populated:
        return {"expected_peak_hour": None, "label": None, "based_on_days": 0,
                "headline": "Not enough history yet."}

    peak_hr, peak_val = max(populated, key=lambda kv: kv[1])
    # Build a 1-hour window centred on the peak (HH:00–HH+1:00).
    label = f"{peak_hr:02d}:00–{peak_hr + 1:02d}:00"
    headline = f"Expected busy time today: {label}"
    return {
        "store_id": store_id,
        "store_name": store.name,
        "expected_peak_hour": peak_hr,
        "label": label,
        "mean_visitors_in_peak": int(round(peak_val / 7)),
        "based_on_days": 7,
        "headline": headline,
    }


@router.get("/chain/hourly-comparison")
def chain_hourly_comparison(db: Session = Depends(get_db),
                            _u=Depends(get_current_user)):
    """Per-store hourly footfall today — one row per store, hours
    09..20. Powers the cross-store comparison chart on /chain. Heavy
    DB scan; reads MetricSnapshot once per store and groups in Python."""
    from sqlalchemy import extract
    stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    cameras_by_store: dict[int, list[int]] = {}
    cam_rows = db.query(Camera.id, Camera.store_id).all()
    for cid, sid in cam_rows:
        if sid is not None:
            cameras_by_store.setdefault(sid, []).append(cid)

    # Single aggregated query — one DB hit instead of N.
    all_cam_ids = [c for cs in cameras_by_store.values() for c in cs]
    rows: list = []
    if all_cam_ids:
        rows = (db.query(MetricSnapshot.camera_id,
                         extract("hour", MetricSnapshot.period_start).label("h"),
                         func.sum(MetricSnapshot.value).label("v"))
                  .filter(MetricSnapshot.camera_id.in_(all_cam_ids),
                          MetricSnapshot.metric_type == "visitor_count_in",
                          MetricSnapshot.period_start >= midnight,
                          MetricSnapshot.period_start <  now)
                  .group_by(MetricSnapshot.camera_id, "h").all())
    cam_to_store = {cid: sid for cid, sid in cam_rows if sid is not None}
    by_store_hour: dict[tuple[int, int], float] = {}
    for cam_id, h, v in rows:
        sid = cam_to_store.get(int(cam_id))
        if sid is None:
            continue
        key = (sid, int(h))
        by_store_hour[key] = by_store_hour.get(key, 0.0) + float(v or 0)

    HOURS = list(range(9, 21))   # 09..20 → 12 points
    out_stores: list[dict] = []
    for s in stores:
        series = [int(round(by_store_hour.get((s.id, hr), 0.0))) for hr in HOURS]
        out_stores.append({
            "store_id": s.id,
            "store_name": s.name,
            "country": s.country,
            "series": series,
            "today_total": sum(series),
        })
    out_stores.sort(key=lambda r: r["today_total"], reverse=True)
    return {
        "hours": [f"{h:02d}:00" for h in HOURS],
        "stores": out_stores,
    }


@router.get("/store/{store_id}/health-score")
def store_health_score(store_id: int, db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    """Composite 0-100 score per the May-2026 spec weights:
       Staff presence %    30%
       Queue wait vs target 20%
       Visitor trend vs 7d 20%
       Incident count       20%
       Camera uptime %      10%
    Last-week comparison provides the trend arrow."""
    from app.utils.business_hours import todays_session
    from app.stream.frame_buffer import FrameBuffer
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"score": None, "note": "no cameras attached"}

    now = datetime.now(timezone.utc)
    today_open, today_close = todays_session(store, now)
    today_end = min(now, today_close)

    def _avg(metric: str, t0, t1) -> float:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id)
                        | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start < t1).scalar())
        return float(v or 0)

    def _sum(metric: str, t0, t1) -> float:
        v = (db.query(func.sum(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store_id)
                        | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start < t1).scalar())
        return float(v or 0)

    # Staff presence % (0-100). Direct map.
    staff_pct = _avg("staff_present_pct", today_open, today_end) * 100
    staff_score = min(100, max(0, staff_pct))

    # Queue wait vs 180s target — 100 at <=target, 0 at 2× target.
    queue_avg_sec = _avg("queue_wait_seconds", today_open, today_end)
    if queue_avg_sec <= 180:
        queue_score = 100.0
    elif queue_avg_sec >= 360:
        queue_score = 0.0
    else:
        queue_score = 100 - ((queue_avg_sec - 180) / 1.8)

    # Visitor trend vs 7-day average for today's window-of-day.
    today_visitors = _sum("visitor_count_in", today_open, today_end)
    week_start = today_open - timedelta(days=7)
    week_avg_visitors = (
        _sum("visitor_count_in", week_start, today_open) / 7 if today_visitors >= 0 else 0
    )
    if week_avg_visitors == 0:
        visitor_score = 50.0    # no baseline → neutral
    else:
        ratio = today_visitors / week_avg_visitors
        # 1.0× → 80, 1.2× → 100, 0.6× → 0
        visitor_score = min(100, max(0, 80 + (ratio - 1.0) * 100))

    # Incidents — fewer is better. Score caps at 100 (0 alerts) and
    # falls 20 per critical alert this week.
    crit_types = ("intrusion", "fight", "weapon", "weapon_brandished",
                  "shrinkage", "fire", "fall")
    week_critical = (
        db.query(func.count(Alert.id))
          .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  Alert.created_at >= today_open - timedelta(days=7),
                  DetectionEvent.detection_type.in_(crit_types)).scalar() or 0
    )
    incident_score = max(0, 100 - week_critical * 20)

    # Camera uptime — fraction of attached cameras with a fresh frame
    # in Redis right now. One MGET for all cameras (was one GET each).
    fb = FrameBuffer()
    health = fb.health_many([c.id for c in cams])
    online = 0
    for cam in cams:
        h = health.get(cam.id) or {}
        lfa = h.get("last_frame_at")
        if lfa and (now.timestamp() - float(lfa)) < 60:
            online += 1
    uptime_pct = (online / len(cams) * 100) if cams else 0
    uptime_score = min(100, max(0, uptime_pct))

    # Weighted composite.
    score = (
        0.30 * staff_score
        + 0.20 * queue_score
        + 0.20 * visitor_score
        + 0.20 * incident_score
        + 0.10 * uptime_score
    )
    score = int(round(score))

    # Last-week comparable score (rough): same formula with last
    # week's averages. Cheap re-use of the same logic with a shifted
    # window — accurate enough for the dashboard's up/down arrow.
    last_week_open  = today_open  - timedelta(days=7)
    last_week_close = today_close - timedelta(days=7)
    lw_staff = _avg("staff_present_pct", last_week_open, last_week_close) * 100
    lw_score = int(round(min(100, max(0, lw_staff))))   # cheap proxy

    components = [
        {"name": "Staff presence",   "weight": 30, "value": round(staff_score, 1)},
        {"name": "Queue wait",       "weight": 20, "value": round(queue_score, 1)},
        {"name": "Visitor trend",    "weight": 20, "value": round(visitor_score, 1)},
        {"name": "Incidents",        "weight": 20, "value": round(incident_score, 1)},
        {"name": "Camera uptime",    "weight": 10, "value": round(uptime_score, 1)},
    ]

    delta = score - lw_score
    arrow = "↑" if delta > 1 else "↓" if delta < -1 else "→"
    return {
        "store_id": store_id,
        "store_name": store.name,
        "score": score,
        "last_week_score": lw_score,
        "delta": delta,
        "headline": f"{store.name}: {score}/100 {arrow} from {lw_score} last week",
        "components": components,
    }


@router.get("/chain/health-leaderboard")
@cached_store_endpoint("chain-health-leaderboard", ttl=60)
def chain_health_leaderboard(db: Session = Depends(get_db),
                              _u=Depends(get_current_user)):
    """Composite health score for every active store — for /chain."""
    stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
    leaderboard = []
    for s in stores:
        try:
            payload = store_health_score(s.id, db=db, _u=_u)  # type: ignore[arg-type]
            if payload.get("score") is None:
                continue
            leaderboard.append({
                "store_id":   s.id,
                "store_name": s.name,
                "country":    s.country,
                "score":      payload["score"],
                "delta":      payload["delta"],
                "components": payload["components"],
            })
        except Exception:
            continue
    leaderboard.sort(key=lambda r: -r["score"])
    return {"leaderboard": leaderboard}


@router.get("/store/{store_id}/customer-flow")
def store_customer_flow(store_id: int,
                        since: datetime | None = None,
                        until: datetime | None = None,
                        db: Session = Depends(get_db),
                        _u=Depends(get_current_user)):
    """Customer movement edges for the heatmap's 'Flow' mode.

    Aggregates over CustomerJourney.zone_sequence_json for the
    selected window. For every pair of CONSECUTIVE zone visits in a
    track's path, increments the edge (from_zone → to_zone).
    Returns:
      • zones: [{ zone_id, name, cx, cy }]   — centroid in 0..1 norm
                                               space (matches the
                                               heatmap PNG coords).
      • edges: [{ from, to, count }]         — sorted desc by count.

    The frontend draws SVG arrows over the heatmap PNG; arrow
    thickness scales with edge count.

    Without per-camera coordinate calibration, all zones from all
    cameras share one normalised 0..1 plane. That's lossy
    geometrically but operators still get the topological structure
    (entry → aisle → counter, etc.) which is what they actually want.
    """
    from app.models import CustomerJourney, Zone
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"zones": [], "edges": [], "total_journeys": 0}

    now = datetime.now(timezone.utc)
    if since is not None:
        t0 = since
        t1 = until or now
    else:
        t0 = now - timedelta(days=7)
        t1 = now

    # Zone metadata — centroid of the polygon for the arrow endpoints.
    zone_rows = (db.query(Zone)
                   .filter(Zone.camera_id.in_(cam_ids)).all())

    def _centroid(poly: list) -> tuple[float, float]:
        """Polygon centroid in normalised image space. Falls back to
        the arithmetic mean of vertices for non-convex shapes — good
        enough for arrow endpoints."""
        if not poly or not isinstance(poly, list) or len(poly) < 1:
            return (0.5, 0.5)
        xs = [p[0] for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [p[1] for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not xs or not ys:
            return (0.5, 0.5)
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    zones_out = []
    zone_centroid: dict[int, tuple[float, float]] = {}
    for z in zone_rows:
        cx, cy = _centroid(z.polygon_coords_json or [])
        zone_centroid[z.id] = (cx, cy)
        zones_out.append({
            "zone_id": z.id, "name": z.name,
            "cx": round(cx, 3), "cy": round(cy, 3),
        })

    # Journeys in the window.
    journeys = (db.query(CustomerJourney)
                  .filter(CustomerJourney.store_id == store_id,
                          CustomerJourney.started_at >= t0,
                          CustomerJourney.started_at <  t1)
                  .limit(5000).all())

    # Edge counts. Each step in zone_sequence_json is the
    # {zone_id, zone_name, entered_at, exited_at} dict that the
    # detector writes. Collapse adjacent duplicates first (a track
    # that re-enters the same zone twice in a row shouldn't make
    # a self-loop edge).
    edges: dict[tuple[int, int], int] = {}
    for j in journeys:
        seq = j.zone_sequence_json or []
        if len(seq) < 2:
            continue
        ids: list[int] = []
        for step in seq:
            if isinstance(step, dict):
                zid = step.get("zone_id")
            else:
                zid = None
            if zid is None:
                continue
            if not ids or ids[-1] != zid:
                ids.append(zid)
        for a, b in zip(ids, ids[1:]):
            edges[(a, b)] = edges.get((a, b), 0) + 1

    edges_out = sorted(
        ({"from": a, "to": b, "count": n} for (a, b), n in edges.items()),
        key=lambda e: -e["count"],
    )[:50]      # cap — drawing more than 50 arrows is unreadable anyway

    return {
        "zones": zones_out,
        "edges": edges_out,
        "total_journeys": len(journeys),
        "window": {"since": t0.isoformat(), "until": t1.isoformat()},
    }


@router.get("/store/{store_id}/loss-prevention")
def store_loss_prevention(store_id: int, since: datetime | None = None, until: datetime | None = None, db: Session = Depends(get_db),
                          _u=Depends(get_current_user)):
    """Daily LP summary: time-of-day pattern, camera hotspots, staff
    correlation. Pure aggregation over DetectionEvent + Alert rows
    of shrinkage / loitering / weapon types."""
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"summary": [], "note": "no cameras attached"}

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    LP_TYPES = ("shrinkage", "loitering", "weapon", "weapon_brandished",
                "abandoned_object")

    events = (
        db.query(DetectionEvent, Camera)
          .outerjoin(Camera, DetectionEvent.camera_id == Camera.id)
          .filter(DetectionEvent.camera_id.in_(cam_ids),
                  DetectionEvent.detection_type.in_(LP_TYPES),
                  DetectionEvent.timestamp >= week_start)
          .all()
    )
    total = len(events)

    # Hour buckets
    by_hour: dict[int, int] = {}
    for ev, _cam in events:
        h = ev.timestamp.hour
        by_hour[h] = by_hour.get(h, 0) + 1
    peak_hour = max(by_hour.items(), key=lambda kv: kv[1])[0] if by_hour else None
    peak_hour_count = by_hour.get(peak_hour, 0) if peak_hour is not None else 0

    # Camera hotspots
    by_cam: dict[int, dict] = {}
    for ev, cam in events:
        if ev.camera_id is None:
            continue
        d = by_cam.setdefault(ev.camera_id, {
            "camera_id": ev.camera_id,
            "camera_name": cam.name if cam else f"Camera {ev.camera_id}",
            "count": 0,
        })
        d["count"] += 1
    hotspots = sorted(by_cam.values(), key=lambda d: -d["count"])[:5]
    top = hotspots[0] if hotspots else None
    top_share = round(top["count"] / total * 100, 1) if (top and total) else 0

    # Correlation with unstaffed counter: fraction of alerts whose
    # time falls inside a known staff gap. Rough but actionable.
    # Pre-compute unstaffed minutes from staff_present_pct as a set
    # of timestamps where value < 0.5. Expensive on long windows, so
    # use today only.
    today_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
    unstaffed_mins = set()
    rows = (db.query(MetricSnapshot.period_start, MetricSnapshot.value)
              .filter(((MetricSnapshot.store_id == store_id)
                       | MetricSnapshot.camera_id.in_(cam_ids)),
                      MetricSnapshot.metric_type == "staff_present_pct",
                      MetricSnapshot.period_start >= today_open,
                      MetricSnapshot.period_start < now).all())
    for ts, val in rows:
        if float(val or 1) < 0.5:
            unstaffed_mins.add(ts.replace(second=0, microsecond=0))
    today_events = [(ev, cam) for ev, cam in events if ev.timestamp >= today_open]
    alerts_today = len(today_events)
    alerts_during_unstaffed = sum(
        1 for ev, _ in today_events
        if ev.timestamp.replace(second=0, microsecond=0) in unstaffed_mins
    )
    ratio_multiplier = None
    if alerts_today > 0:
        unstaffed_share = (alerts_during_unstaffed / alerts_today)
        baseline_share  = (len(unstaffed_mins) / max(1, (now - today_open).total_seconds() / 60))
        if baseline_share > 0:
            ratio_multiplier = round(unstaffed_share / baseline_share, 1)

    return {
        "store_id": store_id,
        "store_name": store.name,
        "total_alerts_this_week": total,
        "peak_hour":     {"hour": peak_hour, "count": peak_hour_count} if peak_hour is not None else None,
        "hotspots":      hotspots,
        "headline_time": (f"Most alerts: {peak_hour:02d}:00-{peak_hour+1:02d}:00"
                          if peak_hour is not None else "No LP alerts this week."),
        "headline_camera": (f"{top['camera_name']} = {top_share}% of LP alerts"
                            if top else None),
        "headline_staff": (f"Alerts {ratio_multiplier}× higher when counter is unstaffed"
                           if ratio_multiplier and ratio_multiplier >= 1.5 else None),
    }


@router.get("/store/{store_id}/behaviour-trends")
@cached_store_endpoint("store-behaviour-trends", ttl=600)
def store_behaviour_trends(store_id: int, since: datetime | None = None, until: datetime | None = None, db: Session = Depends(get_db),
                            _u=Depends(get_current_user)):
    """Weekly customer-behaviour trends — avg visit duration, top
    zones this week vs last, conversion-funnel (entered → counter)."""
    from app.models import CustomerJourney
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "store not found")
    cams = db.query(Camera).filter(Camera.store_id == store_id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return {"note": "no cameras attached"}

    now = datetime.now(timezone.utc)
    if since is not None:
        this_week_start = since
        this_week_end   = until or now
        # Comparison: previous same-length window.
        window_len = this_week_end - this_week_start
        last_week_start = this_week_start - window_len
        last_week_end   = this_week_start
    else:
        this_week_start = now - timedelta(days=7)
        this_week_end   = now
        last_week_start = now - timedelta(days=14)
        last_week_end   = now - timedelta(days=7)

    def _journeys(t0, t1):
        return (db.query(CustomerJourney)
                  .filter(CustomerJourney.store_id == store_id,
                          CustomerJourney.started_at >= t0,
                          CustomerJourney.started_at < t1).all())
    this = _journeys(this_week_start, this_week_end)
    last = _journeys(last_week_start, last_week_end)

    def _avg_dur(rows):
        durs = [(j.ended_at - j.started_at).total_seconds()
                for j in rows if j.ended_at and j.started_at]
        return round(sum(durs) / len(durs), 0) if durs else 0

    this_dur = _avg_dur(this)
    last_dur = _avg_dur(last)
    dur_delta_pct = round((this_dur - last_dur) / last_dur * 100, 1) if last_dur else None

    def _zone_counts(rows):
        counts: dict[str, int] = {}
        for j in rows:
            for step in (j.zone_sequence_json or []):
                z = step.get("zone_name") if isinstance(step, dict) else None
                if z:
                    counts[z] = counts.get(z, 0) + 1
        return counts
    this_zones = _zone_counts(this)
    last_zones = _zone_counts(last)
    top_this = sorted(this_zones.items(), key=lambda kv: -kv[1])[:5]
    top_last = sorted(last_zones.items(), key=lambda kv: -kv[1])[:5]

    # Conversion funnel — entered → reached counter
    def _entered_count(rows):
        return sum(1 for j in rows if (j.zone_sequence_json or []))
    def _converted(rows):
        c = 0
        for j in rows:
            for step in (j.zone_sequence_json or []):
                name = (step.get("zone_name") or "" if isinstance(step, dict) else "").lower()
                if "counter" in name or "checkout" in name:
                    c += 1
                    break
        return c
    this_entered = _entered_count(this)
    this_conv    = _converted(this)
    last_entered = _entered_count(last)
    last_conv    = _converted(last)
    this_conv_pct = round(this_conv / this_entered * 100, 1) if this_entered else 0
    last_conv_pct = round(last_conv / last_entered * 100, 1) if last_entered else 0

    return {
        "store_id": store_id,
        "store_name": store.name,
        "avg_visit_seconds": {
            "this_week": this_dur, "last_week": last_dur,
            "delta_pct": dur_delta_pct,
        },
        "top_zones_this_week": [{"zone": k, "visits": v} for k, v in top_this],
        "top_zones_last_week": [{"zone": k, "visits": v} for k, v in top_last],
        "conversion_funnel": {
            "this_week_pct": this_conv_pct,
            "last_week_pct": last_conv_pct,
            "headline": (f"{this_conv_pct}% of visitors reached counter "
                         f"({'↑' if this_conv_pct > last_conv_pct else '↓' if this_conv_pct < last_conv_pct else '→'} "
                         f"from {last_conv_pct}%)"),
        },
    }


@router.get("/cameras/cv-quality")
def cameras_cv_quality(db: Session = Depends(get_db),
                       _u=Depends(get_current_user)):
    """Average detection confidence per camera over the last 24 hours.
    Cameras whose average confidence is <0.6 are flagged — usually
    lens dirty, focus off, or framing wrong."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    rows = (
        db.query(DetectionEvent.camera_id,
                 func.avg(DetectionEvent.confidence).label("avg_conf"),
                 func.count(DetectionEvent.id).label("n"))
          .filter(DetectionEvent.timestamp >= cutoff,
                  DetectionEvent.confidence.isnot(None))
          .group_by(DetectionEvent.camera_id).all()
    )
    cams = {c.id: c for c in db.query(Camera).all()}
    out = []
    for r in rows:
        cam = cams.get(r.camera_id)
        if not cam:
            continue
        conf = float(r.avg_conf or 0)
        out.append({
            "camera_id":   cam.id,
            "camera_name": cam.name,
            "store_id":    cam.store_id,
            "avg_confidence": round(conf, 2),
            "sample_count": int(r.n),
            "flag": conf < 0.6,
            "hint": (
                "Low confidence — check lens cleanliness, focus, and lighting."
                if conf < 0.6 else None
            ),
        })
    out.sort(key=lambda r: r["avg_confidence"])
    return {"cameras": out, "flagged_count": sum(1 for r in out if r["flag"])}


# ====================================================================
# Chain dashboard revamp — Commit 3 of the dashboard rework.
# ====================================================================
# Two new endpoints that power the Store Opening Status board and
# the Top Issues panel on /chain. Both run cheap aggregates on
# detection_events for the current EAT day, so no heavy joins.


def _eat_now():
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Nairobi"))
    except Exception:
        return datetime.now(timezone.utc)


@router.get("/chain/opening-status")
@cached_store_endpoint("chain-opening-status", ttl=60)
def chain_opening_status(db: Session = Depends(get_db),
                         _u=Depends(get_current_user)):
    """Per-store opening status for today (EAT). Drives the Store
    Opening Status board on /chain.

    For each active store returns one of:
      on_time     — opened at or before the late threshold
      late        — opened after the late threshold, includes minutes_late
      not_opened  — past the not-opened cutoff with no inward crossing
      pending     — before the late threshold, no crossing yet
      no_data     — store has no entry/exit camera or detection events
    """
    from zoneinfo import ZoneInfo
    eat = ZoneInfo("Africa/Nairobi")
    now_local = _eat_now()
    today_local_00 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_local_00.astimezone(timezone.utc)
    today_end_utc   = (today_local_00 + timedelta(days=1)).astimezone(timezone.utc)
    # Match the shop_state defaults (also configurable via the
    # entry_exit detector_config.extra). Hard-coding them here is
    # acceptable because the chain summary just needs to colour a
    # row — operators tune per-store thresholds in detection_configs.
    from datetime import time
    from app.models import Zone
    not_opened_cutoff = time(9, 30)

    stores = (db.query(Store).filter(Store.is_active == True)  # noqa: E712
                .order_by(Store.name).all())
    if not stores:
        return {"as_of": now_local.isoformat(), "stores": []}

    # One pass for cameras (map store_id → has-entry/exit-line).
    has_entry_zone: dict[int, bool] = {}
    cam_rows = db.query(Camera, Zone).join(
        Zone, Zone.camera_id == Camera.id, isouter=True
    ).filter(Camera.ai_enabled == True).all()  # noqa: E712
    for cam, zone in cam_rows:
        if (cam.store_id and zone
                and zone.shape == "line"
                and "entry_exit" in (zone.detection_types_json or [])):
            has_entry_zone[cam.store_id] = True

    # All shop_open_close events today, batched per-store.
    ev_rows = (db.query(DetectionEvent, Camera)
                 .join(Camera, Camera.id == DetectionEvent.camera_id)
                 .filter(DetectionEvent.detection_type == "shop_open_close",
                         DetectionEvent.timestamp >= today_start_utc,
                         DetectionEvent.timestamp <  today_end_utc)
                 .all())
    first_open_by_store: dict[int, DetectionEvent] = {}
    for ev, cam in ev_rows:
        if cam.store_id is None:
            continue
        rule = (ev.extra or {}).get("rule", "")
        if rule not in ("shop_opened", "shop_opened_late"):
            continue
        prev = first_open_by_store.get(cam.store_id)
        if prev is None or ev.timestamp < prev.timestamp:
            first_open_by_store[cam.store_id] = ev

    out = []
    for s in stores:
        opened = first_open_by_store.get(s.id)
        if not has_entry_zone.get(s.id):
            out.append({
                "store_id": s.id, "store_name": s.name,
                "status": "no_data",
                "status_label": "No entry/exit camera configured",
                "opened_at_eat": None, "minutes_late": None,
            })
            continue
        if opened is not None:
            ts = opened.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            local = ts.astimezone(eat)
            rule = (opened.extra or {}).get("rule", "")
            late = rule == "shop_opened_late"
            mins_late = None
            if late:
                opened_mins = local.hour * 60 + local.minute
                start_mins  = 9 * 60       # trading_start_eat default
                mins_late = max(0, opened_mins - start_mins)
            out.append({
                "store_id": s.id, "store_name": s.name,
                "status": "late" if late else "on_time",
                "status_label": (f"Opened {local.strftime('%-I:%M %p')} "
                                 + (f"({mins_late} min late)" if late else "(on time)")),
                "opened_at_eat": local.strftime("%H:%M"),
                "minutes_late": mins_late,
            })
            continue
        # No crossing today — late or pending depends on the clock.
        t_now = now_local.time()
        if t_now > not_opened_cutoff:
            out.append({
                "store_id": s.id, "store_name": s.name,
                "status": "not_opened",
                "status_label": "Not yet opened",
                "opened_at_eat": None, "minutes_late": None,
            })
        else:
            out.append({
                "store_id": s.id, "store_name": s.name,
                "status": "pending",
                "status_label": "Awaiting first crossing",
                "opened_at_eat": None, "minutes_late": None,
            })
    return {"as_of": now_local.isoformat(), "stores": out}


# ----- Top issues across chain --------------------------------------

# Detection types we want to surface on the Top Issues panel,
# mapped to plain-English labels. Heartbeat / informational types
# are deliberately excluded.
_TOP_ISSUE_LABELS: dict[str, str] = {
    "staff_present":      "Counter left unattended",
    "queue":              "Long queues",
    "queue_length":       "Long queues",
    "uniform_compliance": "Staff not in uniform",
    "shop_open_close":    "Late or missed opening",
    "staff_zone":         "Unauthorised person behind counter",
    "intrusion":          "After-hours intrusion",
    "trespass":           "Restricted area entered",
    "fight":              "Incident detected",
    "fall":               "Person fall detected",
    "shrinkage":          "Suspected shoplifting",
    "crowd":              "Crowding",
    "loitering":          "Loitering",
    "abandoned_object":   "Unattended item",
    "tailgating":         "Tailgating",
    "weapon":             "Weapon detected",
    "weapon_brandished":  "Weapon drawn",
    "fire":               "Fire detected",
    "smoke":              "Smoke detected",
}


@router.get("/chain/top-issues")
@cached_store_endpoint("chain-top-issues", ttl=60)
def chain_top_issues(db: Session = Depends(get_db),
                     _u=Depends(get_current_user),
                     limit: int = 5):
    """Top alert types across all stores today (EAT) with the
    most-affected stores per type. Powers the Top Issues card on
    /chain. Informational heartbeats (sales_floor_insight, routine
    shop_opened, entry_exit, dwell, passersby) are excluded so the
    list reads as actionable."""
    from collections import Counter, defaultdict
    now_local = _eat_now()
    today_local_00 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_local_00.astimezone(timezone.utc)
    today_end_utc   = (today_local_00 + timedelta(days=1)).astimezone(timezone.utc)

    # For shop_open_close we only count the actionable rules — routine
    # shop_opened / shop_closed alerts are heartbeats, not issues.
    actionable_shop_rules = ("shop_opened_late", "shop_not_opened",
                             "shop_opened_before_hours")

    rows = (db.query(Alert, DetectionEvent, Camera, Store)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .outerjoin(Camera, DetectionEvent.camera_id == Camera.id)
              .outerjoin(Store,  Camera.store_id == Store.id)
              .filter(Alert.created_at >= today_start_utc,
                      Alert.created_at <  today_end_utc)
              .all())

    type_counts: Counter = Counter()
    per_type_per_store: dict[str, Counter] = defaultdict(Counter)
    minutes_late_by_store: dict[int, int] = {}     # for "Late opening"
    for alert, ev, cam, store in rows:
        dt = ev.detection_type
        if dt not in _TOP_ISSUE_LABELS:
            continue
        # Filter heartbeat-ish shop_open_close rules.
        if dt == "shop_open_close":
            rule = (ev.extra or {}).get("rule", "")
            if rule not in actionable_shop_rules:
                continue
            if rule == "shop_opened_late" and store is not None:
                mins = (ev.extra or {}).get("minutes_late") or 0
                minutes_late_by_store[store.id] = max(
                    minutes_late_by_store.get(store.id, 0), int(mins) or 0)
        type_counts[dt] += 1
        if store is not None:
            per_type_per_store[dt][store.id] += 1

    # Build the response — top N types, with the top 2 most-affected
    # stores rendered inline.
    store_names = {s.id: s.name for s in db.query(Store).all()}
    severity_for = {
        "shrinkage": "high", "intrusion": "high",
        "fight": "critical", "fall": "critical", "weapon": "critical",
        "weapon_brandished": "critical", "fire": "critical", "smoke": "critical",
        "staff_present": "high", "staff_zone": "high",
        "queue": "medium", "queue_length": "medium",
        "uniform_compliance": "medium", "crowd": "medium",
        "shop_open_close": "medium", "loitering": "medium",
        "abandoned_object": "high", "tailgating": "medium",
        "trespass": "high",
    }

    top = []
    for dt, total in type_counts.most_common(limit):
        affected = per_type_per_store[dt].most_common(2)
        affected_text = ", ".join(
            f"{store_names.get(sid, f'Store {sid}')} ({n}x)"
            for sid, n in affected)
        rec = {
            "detection_type": dt,
            "label":          _TOP_ISSUE_LABELS[dt],
            "count":          total,
            "severity":       severity_for.get(dt, "medium"),
            "affected_stores": [
                {"store_id": sid, "store_name": store_names.get(sid),
                 "count": n}
                for sid, n in affected],
            "affected_text": affected_text or "—",
        }
        if dt == "shop_open_close" and minutes_late_by_store:
            rec["late_stores"] = [
                {"store_id": sid,
                 "store_name": store_names.get(sid),
                 "minutes_late": m}
                for sid, m in sorted(minutes_late_by_store.items(),
                                     key=lambda kv: -kv[1])]
        top.append(rec)

    return {"as_of": now_local.isoformat(), "top_issues": top}


# ====================================================================
# ROI / Value Report — Commit 4 of the dashboard rework.
# ====================================================================

@router.get("/roi")
@cached_store_endpoint("roi-report", ttl=300)
def roi_report(db: Session = Depends(get_db),
               _u=Depends(get_current_user)):
    """Month-to-date "VivoGuard Value Report" — count of incidents
    caught + an estimated value, derived from real alert / detection
    data with per-incident KES values from settings.

    All money figures are estimates. The per-incident values are
    deliberately conservative; head-office can tune them via
    .env (ROI_THEFT_PER_INCIDENT_KES, ROI_UNAUTHORISED_PER_INCIDENT_KES,
    ROI_QUEUE_PER_INCIDENT_KES, ROI_MONTHLY_COST_KES).
    """
    from sqlalchemy import func as _func
    from app.config import settings as _settings
    now_local = _eat_now()
    month_start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_utc   = month_start_local.astimezone(timezone.utc)
    prev_month_end_local = month_start_local
    prev_month_start_local = (month_start_local - timedelta(days=1)
                              ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_start_utc = prev_month_start_local.astimezone(timezone.utc)
    prev_month_end_utc   = prev_month_end_local.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    def _count(types, status_in=None, t0=month_start_utc, t1=now_utc):
        q = (db.query(_func.count(Alert.id))
               .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
               .filter(DetectionEvent.detection_type.in_(types),
                       Alert.created_at >= t0,
                       Alert.created_at <  t1))
        if status_in:
            q = q.filter(Alert.status.in_(status_in))
        return int(q.scalar() or 0)

    theft_alerts        = _count(("shrinkage",))
    theft_confirmed     = _count(("shrinkage",), status_in=("resolved", "confirmed"))
    unauthorised        = _count(("trespass", "intrusion", "staff_zone"))
    queue_resolved      = _count(("queue", "queue_length"),
                                 status_in=("resolved", "confirmed"))

    # Staff compliance change: average uniform_compliance_pct this
    # month vs last. Returns a fraction (0..1); we report the
    # percentage-point delta.
    def _avg_uniform(t0, t1):
        v = (db.query(_func.avg(MetricSnapshot.value))
               .filter(MetricSnapshot.metric_type == "uniform_compliance_pct",
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1).scalar())
        return float(v or 0.0)
    cur_uniform  = _avg_uniform(month_start_utc, now_utc) * 100.0
    prev_uniform = _avg_uniform(prev_month_start_utc, prev_month_end_utc) * 100.0
    uniform_delta_pts = round(cur_uniform - prev_uniform, 1)

    # Estimated KES values — defaults are conservative; head-office
    # tunes via env. "Value of theft prevention" uses CONFIRMED
    # shrinkage alerts so we don't inflate the figure with false
    # positives.
    v_theft  = theft_confirmed * int(_settings.roi_theft_per_incident_kes)
    v_unauth = unauthorised    * int(_settings.roi_unauthorised_per_incident_kes)
    v_ops    = queue_resolved  * int(_settings.roi_queue_per_incident_kes)
    total_value   = v_theft + v_unauth + v_ops
    monthly_cost  = int(_settings.roi_monthly_cost_kes)
    roi_multiple  = round(total_value / monthly_cost, 1) if monthly_cost else None

    return {
        "month_label": now_local.strftime("%B %Y"),
        "as_of":       now_local.isoformat(),
        "incidents": {
            "theft_alerts":         theft_alerts,
            "theft_confirmed":      theft_confirmed,
            "unauthorised_access":  unauthorised,
            "queue_issues_resolved": queue_resolved,
            "uniform_compliance_change_pts": uniform_delta_pts,
        },
        "estimated_value_kes": {
            "theft_prevention":     v_theft,
            "operational_savings":  v_ops + v_unauth,
            "total":                total_value,
        },
        "monthly_cost_kes":         monthly_cost,
        "roi_multiple":             roi_multiple,
        "per_incident_kes": {
            "theft":         int(_settings.roi_theft_per_incident_kes),
            "unauthorised":  int(_settings.roi_unauthorised_per_incident_kes),
            "queue":         int(_settings.roi_queue_per_incident_kes),
        },
    }


# ---- Inference latency telemetry (Sprint 1.2 PerfTracker) ------------

@router.get("/perf")
def inference_perf(hours: int = 24,
                   db: Session = Depends(get_db),
                   _u=Depends(get_current_user)):
    """Per-camera inference latency over the last `hours` plus a
    chain-wide rollup. Reads InferencePerfLog (the historical
    100-frame windows written by PerfTracker). Each row already holds
    pre-computed total-stage percentiles; the chain `p95`/`p99` here
    are the MAX across cameras' window percentiles (a conservative
    "worst camera" headline), and `avg_ms` is the frame-weighted mean
    — both labelled so the UI doesn't over-claim a true global
    percentile (which would need the raw samples we don't keep)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.models import Camera, InferencePerfLog, Store

    hours = max(1, min(int(hours), 24 * 30))
    since = _dt.now(_tz.utc) - _td(hours=hours)

    rows = (db.query(InferencePerfLog)
              .filter(InferencePerfLog.timestamp >= since)
              .all())
    if not rows:
        return {"window_hours": hours,
                "chain": {"avg_ms": None, "p95_ms": None, "p99_ms": None,
                          "cameras_reporting": 0, "frames": 0},
                "per_camera": [], "slowest": []}

    # Aggregate per camera: frame-weighted avg, max of window p95/p99.
    by_cam: dict[int, dict] = {}
    for r in rows:
        c = by_cam.setdefault(int(r.camera_id), {
            "camera_id": int(r.camera_id), "frames": 0,
            "avg_weighted": 0.0, "p50_ms": 0.0, "p95_ms": 0.0,
            "p99_ms": 0.0, "backend": r.backend, "format": r.format,
            "last_seen": None,
        })
        n = int(r.frame_count or 0)
        c["frames"] += n
        c["avg_weighted"] += float(r.avg_ms or 0.0) * n
        c["p50_ms"] = max(c["p50_ms"], float(r.p50_ms or 0.0))
        c["p95_ms"] = max(c["p95_ms"], float(r.p95_ms or 0.0))
        c["p99_ms"] = max(c["p99_ms"], float(r.p99_ms or 0.0))
        c["backend"] = r.backend or c["backend"]
        c["format"]  = r.format  or c["format"]
        ts = r.timestamp
        if c["last_seen"] is None or (ts and ts > c["last_seen"]):
            c["last_seen"] = ts

    # Names in one query.
    cam_ids = list(by_cam.keys())
    cams = {c.id: c for c in db.query(Camera).filter(Camera.id.in_(cam_ids)).all()}
    store_ids = {c.store_id for c in cams.values() if c.store_id}
    stores = ({s.id: s.name for s in db.query(Store).filter(Store.id.in_(store_ids)).all()}
              if store_ids else {})

    per_camera = []
    total_frames = 0
    weighted_sum = 0.0
    for cid, c in by_cam.items():
        frames = c["frames"] or 1
        avg = round(c["avg_weighted"] / frames, 2)
        total_frames += c["frames"]
        weighted_sum += c["avg_weighted"]
        cam = cams.get(cid)
        per_camera.append({
            "camera_id":   cid,
            "camera_name": cam.name if cam else None,
            "store_name":  stores.get(cam.store_id) if (cam and cam.store_id) else None,
            "p50_ms":      round(c["p50_ms"], 2),
            "p95_ms":      round(c["p95_ms"], 2),
            "p99_ms":      round(c["p99_ms"], 2),
            "avg_ms":      avg,
            "frame_count": c["frames"],
            "backend":     c["backend"],
            "format":      c["format"],
            "last_seen":   c["last_seen"].isoformat() if c["last_seen"] else None,
        })

    per_camera.sort(key=lambda x: (x["p99_ms"] or 0), reverse=True)
    chain_avg = round(weighted_sum / total_frames, 2) if total_frames else None
    return {
        "window_hours": hours,
        "chain": {
            "avg_ms":            chain_avg,
            "p95_ms_worst_cam":  max((c["p95_ms"] for c in per_camera), default=None),
            "p99_ms_worst_cam":  max((c["p99_ms"] for c in per_camera), default=None),
            "cameras_reporting": len(per_camera),
            "frames":            total_frames,
        },
        "per_camera": per_camera,
        "slowest":    per_camera[:5],
    }
