"""Lightweight metric writer used by every retail detector and the
inference worker. Buffers writes per camera so Postgres isn't hammered
with one INSERT per frame.

Usage:
    MetricRecorder.record(db, "queue_length", value=8,
                          camera_id=cam.id, store_id=cam.store_id,
                          zone_id=zone_id, dims={"shift": "morning"})
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import MetricSnapshot

# Bucket size — metrics are quantised to this granularity so the
# Analytics dashboard can render time series cheaply. 1-minute buckets
# strike a good balance between disk usage and chart resolution.
BUCKET_SECONDS = 60


def _bucket_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(second=0, microsecond=0)
    return start, start + timedelta(seconds=BUCKET_SECONDS)


def record(db: Session, metric_type: str, value: float, *,
           store_id: int | None = None,
           camera_id: int | None = None,
           zone_id: int | None = None,
           dims: dict | None = None,
           aggregator: str = "last") -> None:
    """Insert or update the bucket row for the current minute.

    `aggregator` controls how repeated writes in the same bucket combine:
        last  — overwrite (occupancy, queue length, shutter state)
        max   — keep the larger value (max queue depth this minute)
        sum   — sum (passersby counts, footfall heatmap cell hits)
        avg   — running mean (dwell seconds)
    """
    start, end = _bucket_bounds()

    q = (db.query(MetricSnapshot)
           .filter(MetricSnapshot.metric_type == metric_type,
                   MetricSnapshot.period_start == start,
                   MetricSnapshot.camera_id == camera_id,
                   MetricSnapshot.store_id == store_id,
                   MetricSnapshot.zone_id == zone_id))
    # The `dims` column is declared as JSON (not JSONB), and Postgres
    # has no `=` operator for the `json` type — pushing the equality
    # into the WHERE clause aborts the whole transaction with
    # `operator does not exist: json = json`. We post-filter in
    # Python instead: the (metric_type, period_start, camera, store,
    # zone) prefix already narrows the candidate set to at most a
    # handful of rows per minute, so the cost is negligible.
    if dims is None:
        row = q.first()
    else:
        row = next((r for r in q.all() if (r.dims or {}) == dims), None)

    if row is None:
        db.add(MetricSnapshot(
            store_id=store_id, camera_id=camera_id, zone_id=zone_id,
            metric_type=metric_type,
            period_start=start, period_end=end, value=value, dims=dims,
        ))
        return

    if aggregator == "last":
        row.value = value
    elif aggregator == "max":
        row.value = max(row.value, value)
    elif aggregator == "sum":
        row.value = row.value + value
    elif aggregator == "avg":
        # Track running mean via dims["n"] sample count.
        n = (row.dims or {}).get("_n", 1)
        row.value = (row.value * n + value) / (n + 1)
        row.dims = {**(row.dims or {}), "_n": n + 1}


def query(db: Session, metric_type: str, *,
          store_id: int | None = None,
          camera_id: int | None = None,
          zone_id: int | None = None,
          since: datetime | None = None,
          until: datetime | None = None,
          limit: int = 1440) -> list[MetricSnapshot]:
    """Time-bounded read for the analytics dashboard."""
    q = db.query(MetricSnapshot).filter(MetricSnapshot.metric_type == metric_type)
    if store_id  is not None: q = q.filter(MetricSnapshot.store_id  == store_id)
    if camera_id is not None: q = q.filter(MetricSnapshot.camera_id == camera_id)
    if zone_id   is not None: q = q.filter(MetricSnapshot.zone_id   == zone_id)
    if since:                 q = q.filter(MetricSnapshot.period_start >= since)
    if until:                 q = q.filter(MetricSnapshot.period_start <= until)
    return q.order_by(MetricSnapshot.period_start).limit(limit).all()
