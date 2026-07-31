"""Single source of truth for "how many visitors today (or in this
window)?" across the dashboard.

Three places used to disagree on the same store's number:

  /store/{id}/live         unique_visitors_today tile  → VisitorTrack count
  /store/{id}/hourly       Total-today + per-hour bars → metric_snapshots
                                                          (visitor_count_in
                                                           / occupancy)
  Store cards on the chain dashboard → yet another query

The methodologies are honestly different (de-duplicated track-IDs vs.
sum of inward line-crossings vs. peak occupancy), so the numbers
diverge — sometimes by 2-3x. This helper picks ONE source per call
following a clear priority and returns BOTH the daily total AND the
per-hour breakdown derived from the same source, so the hourly bars
always sum to the displayed total.

Time-zone: every per-hour bucket is in the store's local time (EAT
default via Africa/Nairobi) — the chart axis labels are EAT, the
buckets must match. The previous hourly endpoint extracted UTC hours
and labelled them EAT, off-by-three on every Vivo store.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import String, cast, distinct, func
from sqlalchemy.orm import Session

from app.models import MetricSnapshot, StaffTrack, VisitorTrack, Zone

log = logging.getLogger(__name__)


_STORE_TZ_DEFAULT = "Africa/Nairobi"


@dataclass
class VisitorCount:
    by_hour_eat: dict[int, int]                # {0..23: count}
    total: int                                  # = sum(by_hour_eat.values())
    source: str                                 # "unique_visitors"|"visitor_count_in_glass_door"|"visitor_count_in"|"occupancy"|"none"
    source_label: str                           # plain-English for the UI
    stores_querying: dict = field(default_factory=dict)


_SOURCE_LABEL = {
    "unique_visitors":              "Based on unique visitor tracks (de-duplicated, staff excluded)",
    "visitor_count_in_glass_door":  "🚪 High-accuracy count — based on glass-door entry sensor",
    "visitor_count_in":             "Based on entry/exit camera data",
    "occupancy":                    "Based on occupancy data (estimated)",
    "none":                         "No visitor data yet",
}


def _store_tz_name(store) -> str:
    return getattr(store, "timezone", None) or _STORE_TZ_DEFAULT


def _eat_hour_expr(column, tz_name: str):
    """SQL expression that yields the hour-of-day in the store's
    timezone for a UTC-stored timestamp column. Postgres:
        EXTRACT(HOUR FROM (col AT TIME ZONE tz))"""
    return func.extract("hour", func.timezone(tz_name, column))


def _pick_visitor_zone_ids(db: Session, cam_ids: list[int]) -> tuple[list[int], bool]:
    """Return the zone ids that should source visitor counts for the
    store, plus a flag for whether they're the high-accuracy
    glass-door subset.

    Priority: glass_door-tagged LINE zones win when any exist among
    the store's cameras; otherwise plain entry_exit LINE zones are
    used. This prevents double-counting on stores that have both
    types defined on overlapping cameras — a count is attributed to
    the more accurate sensor when there's a choice.

    Returns (zone_ids, is_glass_door). Empty list = no entry_exit
    line zones at all for this store → caller falls back to the
    legacy store/camera filter.
    """
    if not cam_ids:
        return [], False
    zones = (db.query(Zone)
                .filter(Zone.camera_id.in_(cam_ids),
                        Zone.shape == "line")
                .all())
    eligible = [z for z in zones
                if "entry_exit" in (z.detection_types_json or [])]
    glass = [z for z in eligible
             if "glass_door" in (z.detection_types_json or [])]
    if glass:
        return [int(z.id) for z in glass], True
    return [int(z.id) for z in eligible], False


def daily_visitor_count(
    db: Session,
    store,
    *,
    since_utc: datetime,
    until_utc: datetime,
    cam_ids: list[int],
) -> VisitorCount:
    """Pick the best available source for "visitors in this window"
    and return both a daily total and a per-hour breakdown bucketed
    in the store's local timezone.

    Priority:
      1. VisitorTrack (de-duplicated unique people, staff excluded).
         The most honest "how many real customers visited" figure.
      2. visitor_count_in metric (sum of inward entry/exit-line
         crossings). Counts repeated entries.
      3. occupancy (per-hour peak). Loose proxy when neither of the
         above is wired up — the per-hour values are MAXes, not
         counts, so the total here is "sum of hourly peaks" and is
         intentionally fuzzy.

    Hourly bars and the displayed total are derived from the SAME
    source, so they always reconcile.
    """
    tz_name = _store_tz_name(store)
    store_id = int(store.id)

    # ----- 1. unique visitors via VisitorTrack ------------------------
    # Sprint 2.2: when the Re-ID pipeline has stamped a cross-camera
    # global_person_id, count DISTINCT identities so the same shopper seen
    # on several cameras counts ONCE — fixing the long-standing per-camera
    # double-count. Rows without a global id (Re-ID off, or not yet
    # assigned) fall back to their unique row id via COALESCE, so they
    # each still count once exactly as before. The identity is bucketed
    # per hour, so same-hour multi-camera appearances collapse; the
    # hourly bars still sum to the displayed total by construction.
    _visitor_identity = func.coalesce(
        VisitorTrack.global_person_id,
        cast(VisitorTrack.id, String),
    )
    rows = (db.query(_eat_hour_expr(VisitorTrack.first_seen, tz_name).label("h"),
                     func.count(distinct(_visitor_identity)).label("n"))
              .outerjoin(StaffTrack,
                         (StaffTrack.store_id == store_id)
                         & (StaffTrack.day == VisitorTrack.day)
                         & (StaffTrack.track_signature == VisitorTrack.track_signature))
              .filter(
                  ((VisitorTrack.store_id == store_id)
                   | (VisitorTrack.camera_id.in_(cam_ids) if cam_ids else False)),
                  VisitorTrack.first_seen >= since_utc,
                  VisitorTrack.first_seen <  until_utc,
                  (StaffTrack.classified_as.is_(None))
                  | (StaffTrack.classified_as != "staff"),
              )
              .group_by("h").all())
    by_hour = {int(r.h): int(r.n or 0) for r in rows if r.h is not None}
    if sum(by_hour.values()) > 0:
        return VisitorCount(
            by_hour_eat=by_hour, total=sum(by_hour.values()),
            source="unique_visitors",
            source_label=_SOURCE_LABEL["unique_visitors"],
        )

    # ----- 2. visitor_count_in line crossings ------------------------
    # Glass-door priority: when any glass_door entry_exit zone exists
    # for the store, only its crossings are counted. This avoids
    # double-counting on stores that also have a plain entry_exit
    # line on overlapping cameras — and surfaces the higher-accuracy
    # source label to the UI.
    visitor_zone_ids, via_glass = _pick_visitor_zone_ids(db, cam_ids or [])
    base_filters = [
        MetricSnapshot.metric_type == "visitor_count_in",
        MetricSnapshot.period_start >= since_utc,
        MetricSnapshot.period_start <  until_utc,
    ]
    if visitor_zone_ids:
        # Authoritative — the detector writes zone_id on every
        # visitor_count_* row, so this filter is exact.
        scope_filter = MetricSnapshot.zone_id.in_(visitor_zone_ids)
    else:
        # No entry_exit zones drawn — fall back to the legacy
        # store/camera scope so existing tests + historic rows
        # without zone metadata keep working.
        scope_filter = (
            (MetricSnapshot.store_id == store_id)
            | (MetricSnapshot.camera_id.in_(cam_ids) if cam_ids else False)
        )
    rows = (db.query(_eat_hour_expr(MetricSnapshot.period_start, tz_name).label("h"),
                     func.sum(MetricSnapshot.value).label("v"))
              .filter(scope_filter, *base_filters)
              .group_by("h").all())
    by_hour = {int(r.h): int(round(float(r.v or 0))) for r in rows if r.h is not None}
    if sum(by_hour.values()) > 0:
        src = "visitor_count_in_glass_door" if via_glass else "visitor_count_in"
        return VisitorCount(
            by_hour_eat=by_hour, total=sum(by_hour.values()),
            source=src,
            source_label=_SOURCE_LABEL[src],
        )

    # ----- 3. last-resort: per-hour occupancy peaks ------------------
    rows = (db.query(_eat_hour_expr(MetricSnapshot.period_start, tz_name).label("h"),
                     func.max(MetricSnapshot.value).label("v"))
              .filter(
                  ((MetricSnapshot.store_id == store_id)
                   | (MetricSnapshot.camera_id.in_(cam_ids) if cam_ids else False)),
                  MetricSnapshot.metric_type == "occupancy",
                  MetricSnapshot.period_start >= since_utc,
                  MetricSnapshot.period_start <  until_utc,
              )
              .group_by("h").all())
    by_hour = {int(r.h): int(round(float(r.v or 0))) for r in rows if r.h is not None}
    if sum(by_hour.values()) > 0:
        return VisitorCount(
            by_hour_eat=by_hour, total=sum(by_hour.values()),
            source="occupancy",
            source_label=_SOURCE_LABEL["occupancy"],
        )

    return VisitorCount(by_hour_eat={}, total=0, source="none",
                        source_label=_SOURCE_LABEL["none"])


def reconcile_warn(store_name: str, *, hourly_sum: int, daily_total: int,
                    threshold_pct: float = 10.0) -> None:
    """Log a WARNING when two callers of this helper end up with
    materially different numbers — should be impossible by
    construction now that both pull from daily_visitor_count, but
    keeps the alarm wired up for future regressions."""
    if daily_total == 0 and hourly_sum == 0:
        return
    base = max(1, daily_total)
    pct = abs(hourly_sum - daily_total) / base * 100.0
    if pct > threshold_pct:
        log.warning(
            "Visitor count discrepancy: hourly_sum=%d daily_total=%d "
            "store=%s — check entry/exit zone configuration",
            hourly_sum, daily_total, store_name)
