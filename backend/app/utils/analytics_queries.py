"""Shared chain/store KPI aggregation queries.

Single home for the dashboard aggregation SQL used by BOTH
`/analytics/dashboard/multi` (all stores at once) and
`/analytics/dashboard/store/{id}` (one store) — one implementation,
one query plan, no drift between the two endpoints.

Design rules:
  * Core-style tuple queries only — no ORM entity instantiation.
  * One round-trip per concern, GROUP BY store_id, conditional
    aggregation (FILTER) instead of separate queries per bucket.
  * Every function takes `store_ids` so the single-store endpoint is
    just the chain call with a one-element list.
"""
from __future__ import annotations

from datetime import date as date_t
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import Alert, Camera, DetectionEvent, MetricSnapshot, VisitorTrack

AVG_METRICS: tuple[str, ...] = (
    "occupancy", "queue_length", "queue_wait_seconds", "staff_present_pct",
    "passersby", "stop_rate", "dwell_seconds")
SUM_METRICS: tuple[str, ...] = ("visitor_count_in", "visitor_count_out")
LAST_METRICS: tuple[str, ...] = ("occupancy", "queue_length", "shutter_open")

# LIVE latest-sample lookback. Unbounded, the window function sorted the
# whole metric_snapshots table; 48h keeps it an index-range scan and a
# camera silent longer than that honestly reports null "now" KPIs.
LIVE_LAST_LOOKBACK_HOURS = 48


def fetch_metric_aggregates(
    db: Session, store_ids: list[int],
    window_since: datetime, window_until: datetime,
) -> tuple[dict[tuple[int, str], float], dict[tuple[int, str], float]]:
    """(avg_map, sum_map) keyed (store_id, metric_type) — ONE pass over
    metric_snapshots computes both aggregates for the union of metric
    sets; membership decides which map a row lands in."""
    avg_map: dict[tuple[int, str], float] = {}
    sum_map: dict[tuple[int, str], float] = {}
    if not store_ids:
        return avg_map, sum_map
    for sid, mt, avg_v, sum_v in (
            db.query(Camera.store_id, MetricSnapshot.metric_type,
                     func.avg(MetricSnapshot.value),
                     func.sum(MetricSnapshot.value))
              .join(Camera, Camera.id == MetricSnapshot.camera_id)
              .filter(Camera.store_id.in_(store_ids),
                      MetricSnapshot.metric_type.in_(AVG_METRICS + SUM_METRICS),
                      MetricSnapshot.period_start >= window_since,
                      MetricSnapshot.period_start < window_until)
              .group_by(Camera.store_id, MetricSnapshot.metric_type)
              .all()):
        if mt in SUM_METRICS:
            if sum_v is not None:
                sum_map[(sid, mt)] = float(sum_v)
        elif avg_v is not None:
            avg_map[(sid, mt)] = float(avg_v)
    return avg_map, sum_map


def fetch_latest_samples(
    db: Session, store_ids: list[int], *, now: datetime,
    historical_window: tuple[datetime, datetime] | None = None,
) -> dict[tuple[int, str], float]:
    """Latest sample per (store, metric) for the "now" KPI tiles.
    Live path scans only the last LIVE_LAST_LOOKBACK_HOURS; a historical
    window uses its own bounds."""
    last_map: dict[tuple[int, str], float] = {}
    if not store_ids:
        return last_map
    rn = func.row_number().over(
        partition_by=(Camera.store_id, MetricSnapshot.metric_type),
        order_by=MetricSnapshot.period_start.desc())
    q = (db.query(Camera.store_id.label("sid"),
                  MetricSnapshot.metric_type.label("mt"),
                  MetricSnapshot.value.label("val"),
                  rn.label("rn"))
           .join(Camera, Camera.id == MetricSnapshot.camera_id)
           .filter(Camera.store_id.in_(store_ids),
                   MetricSnapshot.metric_type.in_(LAST_METRICS)))
    if historical_window is not None:
        q = q.filter(MetricSnapshot.period_start >= historical_window[0],
                     MetricSnapshot.period_start < historical_window[1])
    else:
        q = q.filter(MetricSnapshot.period_start
                     >= now - timedelta(hours=LIVE_LAST_LOOKBACK_HOURS))
    sub = q.subquery()
    for r in db.query(sub.c.sid, sub.c.mt, sub.c.val).filter(sub.c.rn == 1):
        last_map[(r.sid, r.mt)] = r.val
    return last_map


def fetch_visitor_counts(
    db: Session, store_ids: list[int],
    window_since: datetime, window_until: datetime, today: date_t,
) -> tuple[dict[int, int], dict[int, int]]:
    """(window_map, today_map) — one pass over visitor_tracks with two
    FILTERed counts instead of two separate queries."""
    if not store_ids:
        return {}, {}
    in_window = and_(VisitorTrack.first_seen >= window_since,
                     VisitorTrack.first_seen < window_until)
    is_today = VisitorTrack.day == today
    window_map: dict[int, int] = {}
    today_map: dict[int, int] = {}
    for sid, n_window, n_today in (
            db.query(VisitorTrack.store_id,
                     func.count(VisitorTrack.id).filter(in_window),
                     func.count(VisitorTrack.id).filter(is_today))
              .filter(VisitorTrack.store_id.in_(store_ids),
                      or_(in_window, is_today))
              .group_by(VisitorTrack.store_id)
              .all()):
        window_map[sid] = int(n_window or 0)
        today_map[sid] = int(n_today or 0)
    return window_map, today_map


def fetch_alert_stats(
    db: Session, store_ids: list[int],
    window_since: datetime, window_until: datetime, *,
    now: datetime, critical_types: tuple[str, ...] = (),
) -> tuple[dict[int, dict[str, int]], dict[int, int]]:
    """(alerts_breakdown_map, recent_critical_map) in ONE grouped query.
    breakdown = per-store per-type counts inside the window;
    recent_critical = per-store count of critical_types in the last hour
    (empty critical_types → always {})."""
    ab_map: dict[int, dict[str, int]] = {}
    rc_map: dict[int, int] = {}
    if not store_ids:
        return ab_map, rc_map
    in_window = and_(Alert.created_at >= window_since,
                     Alert.created_at < window_until)
    preds = [in_window]
    crit_hour = None
    if critical_types:
        crit_hour = and_(Alert.created_at >= now - timedelta(hours=1),
                         DetectionEvent.detection_type.in_(critical_types))
        preds.append(crit_hour)
    cols = [Camera.store_id, DetectionEvent.detection_type,
            func.count(Alert.id).filter(in_window)]
    if crit_hour is not None:
        cols.append(func.count(Alert.id).filter(crit_hour))
    for row in (db.query(*cols)
                  .select_from(Alert)
                  .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                  .join(Camera, Camera.id == DetectionEvent.camera_id)
                  .filter(Camera.store_id.in_(store_ids), or_(*preds))
                  .group_by(Camera.store_id, DetectionEvent.detection_type)
                  .all()):
        sid, dt, n_window = row[0], row[1], int(row[2] or 0)
        if n_window:
            ab_map.setdefault(sid, {})[dt] = n_window
        if crit_hour is not None and int(row[3] or 0):
            rc_map[sid] = rc_map.get(sid, 0) + int(row[3])
    return ab_map, rc_map


def fetch_camera_metric_sums(
    db: Session, camera_ids: list[int],
    window_since: datetime, window_until: datetime,
    metrics: tuple[str, ...] = SUM_METRICS,
) -> dict[tuple[int, str], float]:
    """Per-CAMERA metric sums — the camera-grained sibling of
    fetch_metric_aggregates (which groups by store). One pass."""
    out: dict[tuple[int, str], float] = {}
    if not camera_ids:
        return out
    for cid, mt, v in (
            db.query(MetricSnapshot.camera_id, MetricSnapshot.metric_type,
                     func.sum(MetricSnapshot.value))
              .filter(MetricSnapshot.camera_id.in_(camera_ids),
                      MetricSnapshot.metric_type.in_(metrics),
                      MetricSnapshot.period_start >= window_since,
                      MetricSnapshot.period_start < window_until)
              .group_by(MetricSnapshot.camera_id, MetricSnapshot.metric_type)
              .all()):
        if v is not None:
            out[(cid, mt)] = float(v)
    return out


def fetch_camera_latest(
    db: Session, camera_ids: list[int],
    metrics: tuple[str, ...], *, now: datetime,
) -> dict[tuple[int, str], float]:
    """Latest sample per (camera, metric) with the same 48h live
    lookback the store-level fetch_latest_samples uses."""
    out: dict[tuple[int, str], float] = {}
    if not camera_ids:
        return out
    rn = func.row_number().over(
        partition_by=(MetricSnapshot.camera_id, MetricSnapshot.metric_type),
        order_by=MetricSnapshot.period_start.desc())
    sub = (db.query(MetricSnapshot.camera_id.label("cid"),
                    MetricSnapshot.metric_type.label("mt"),
                    MetricSnapshot.value.label("val"),
                    rn.label("rn"))
             .filter(MetricSnapshot.camera_id.in_(camera_ids),
                     MetricSnapshot.metric_type.in_(metrics),
                     MetricSnapshot.period_start
                     >= now - timedelta(hours=LIVE_LAST_LOOKBACK_HOURS))
             .subquery())
    for r in db.query(sub.c.cid, sub.c.mt, sub.c.val).filter(sub.c.rn == 1):
        out[(r.cid, r.mt)] = float(r.val)
    return out
