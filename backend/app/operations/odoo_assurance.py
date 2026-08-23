"""Advisory-only fusion between Odoo projections and CCTV evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Camera, DetectionEvent, MetricSnapshot, OdooConversionMetric,
    OdooPosActivityBucket, OdooPosSession, OdooRosterWindow,
    OdooStoreSalesHourly, OdooTillConflict,
    Store, StoreBusinessHours, Zone,
)
from app.operations.assurance import upsert_case
from app.utils.business_hours import WEEKDAY_KEYS, normalise_business_hours


def ensure_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def pseudonymise_employee(odoo_id: int, secret: str) -> str:
    """Stable opaque reference; the Odoo ID is never persisted directly."""
    return hmac.new(secret.encode(), f"odoo-employee:{odoo_id}".encode(), sha256).hexdigest()


def hours_rows_to_json(rows: list[StoreBusinessHours]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in rows:
        key = WEEKDAY_KEYS[row.day_of_week]
        if row.open_time is None or row.close_time is None:
            result[key] = []
        else:
            result[key] = [f"{row.open_time:%H:%M}-{row.close_time:%H:%M}"]
    return result


def effective_business_hours(
    db: Session, store: Store, at: datetime | None = None, *, max_age_hours: int = 48,
) -> tuple[dict | None, str]:
    """Fresh Odoo rows > explicit manual rows > Store JSON > fleet default.

    Stale Odoo data is ignored, never treated as closed.  Returning ``None``
    delegates to the existing safe fleet default in ``is_open_with_default``.
    """
    at = ensure_aware(at or datetime.now(timezone.utc))
    rows = db.query(StoreBusinessHours).filter(
        StoreBusinessHours.store_id == store.id).all()
    odoo_rows = [r for r in rows if r.source == "odoo" and
                 timedelta(0) <= at - ensure_aware(r.synced_at) <=
                 timedelta(hours=max_age_hours)]
    if odoo_rows:
        return hours_rows_to_json(odoo_rows), "odoo"
    manual_rows = [r for r in rows if r.source == "manual"]
    if manual_rows:
        return hours_rows_to_json(manual_rows), "manual"
    if store.business_hours_json:
        return normalise_business_hours(store.business_hours_json), "store"
    return None, "default"


def roster_advisory(
    db: Session, store_id: int, at: datetime, *, max_age_hours: int = 24,
) -> dict:
    """Return context only. Callers must never suppress an alert from this."""
    at = ensure_aware(at)
    row = (db.query(OdooRosterWindow)
           .filter(OdooRosterWindow.store_id == store_id,
                   OdooRosterWindow.shift_start <= at,
                   OdooRosterWindow.shift_end >= at)
           .order_by(OdooRosterWindow.synced_at.desc()).first())
    if row is None:
        return {"expected_staff_window": "unknown", "alert_suppressed": False}
    age = at - ensure_aware(row.synced_at)
    fresh = timedelta(0) <= age <= timedelta(hours=max_age_hours)
    return {
        "expected_staff_window": "expected" if fresh else "stale",
        "roster_synced_at": ensure_aware(row.synced_at).isoformat(),
        "alert_suppressed": False,
    }


def compute_till_conflicts(db: Session, day, *, tolerance_minutes: int = 30) -> int:
    """Persist both signals and a conflict flag; never alter alert thresholds."""
    count = 0
    tolerance = timedelta(minutes=tolerance_minutes)
    for store in db.query(Store).filter(Store.is_active.is_(True)).all():
        tz = ZoneInfo(store.timezone or "Africa/Nairobi")
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        camera_events = (db.query(DetectionEvent).join(Camera)
                         .filter(Camera.store_id == store.id,
                                 DetectionEvent.detection_type == "shop_open_close",
                                 DetectionEvent.timestamp >= start,
                                 DetectionEvent.timestamp < end).all())
        opened_camera = min((e.timestamp for e in camera_events
                             if (e.extra or {}).get("rule") in {
                                 "shop_opened", "shop_opened_late",
                                 "shop_opened_inferred", "shop_opened_via_occupancy"}), default=None)
        closed_camera = min((e.timestamp for e in camera_events
                             if (e.extra or {}).get("rule") == "shop_closed"), default=None)
        sessions = (db.query(OdooPosSession)
                    .filter(OdooPosSession.store_id == store.id,
                            OdooPosSession.opened_at >= start,
                            OdooPosSession.opened_at < end).all())
        opened_till = min((s.opened_at for s in sessions if s.opened_at), default=None)
        closed_till = max((s.closed_at for s in sessions if s.closed_at), default=None)
        checks = (
            ("opening_signal_conflict", opened_camera, opened_till),
            ("closing_signal_conflict", closed_camera, closed_till),
        )
        for conflict_type, camera_at, till_at in checks:
            conflict = ((camera_at is None) != (till_at is None) or
                        (camera_at is not None and till_at is not None and
                         abs(ensure_aware(camera_at) - ensure_aware(till_at)) > tolerance))
            existing = db.query(OdooTillConflict).filter_by(
                store_id=store.id, business_day=day, conflict_type=conflict_type).one_or_none()
            if conflict:
                if existing is None:
                    existing = OdooTillConflict(store_id=store.id, business_day=day,
                                                conflict_type=conflict_type)
                    db.add(existing)
                existing.camera_event_at = camera_at
                existing.till_event_at = till_at
                existing.status = "open"
                count += 1
            elif existing is not None:
                existing.status = "resolved"
    return count


def compute_conversion_metrics(db: Session, period_start: datetime,
                               *, maximum: float = 0.60) -> int:
    period_start = ensure_aware(period_start).replace(minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(hours=1)
    count = 0
    for sales in db.query(OdooStoreSalesHourly).filter(
            OdooStoreSalesHourly.period_start == period_start).all():
        footfall = int(db.query(func.coalesce(func.sum(MetricSnapshot.value), 0)).filter(
            MetricSnapshot.store_id == sales.store_id,
            MetricSnapshot.metric_type == "unique_visitors",
            MetricSnapshot.period_start >= period_start,
            MetricSnapshot.period_start < period_end).scalar() or 0)
        rate = sales.transaction_count / footfall if footfall > 0 else None
        quality = rate is not None and rate > maximum
        row = db.query(OdooConversionMetric).filter_by(
            store_id=sales.store_id, period_start=period_start).one_or_none()
        if row is None:
            row = OdooConversionMetric(store_id=sales.store_id, period_start=period_start,
                                       footfall=footfall, transactions=sales.transaction_count)
            db.add(row)
        row.footfall = footfall
        row.transactions = sales.transaction_count
        row.conversion_rate = rate
        row.data_quality_flag = quality
        count += 1
    return count


def create_changing_room_reviews(db: Session, now: datetime,
                                 *, grace_minutes: int = 15,
                                 pos_max_age_minutes: int = 30) -> int:
    """Cross-check visual changing-room exits with aggregate POS activity.

    No person identity is inferred and wording is explicitly non-accusatory.
    """
    now = ensure_aware(now)
    since = now - timedelta(minutes=grace_minutes + 30)
    zone_rows = (db.query(DetectionEvent, Camera, Zone)
                 .join(Camera, Camera.id == DetectionEvent.camera_id)
                 .join(Zone, Zone.id == DetectionEvent.zone_id)
                 .filter(DetectionEvent.timestamp >= since,
                         DetectionEvent.detection_type.in_(("entry_exit", "changing_room"))).all())
    count = 0
    for event, camera, zone in zone_rows:
        types = set(zone.detection_types_json or [])
        if "changing_room" not in types:
            continue
        extra = event.extra or {}
        if extra.get("direction") not in {"out", "exit", "outward"}:
            continue
        review_at = ensure_aware(event.timestamp) + timedelta(minutes=grace_minutes)
        if review_at > now or camera.store_id is None:
            continue
        sale = (db.query(OdooPosActivityBucket)
                .filter(OdooPosActivityBucket.store_id == camera.store_id,
                        OdooPosActivityBucket.period_start <= review_at,
                        OdooPosActivityBucket.period_start >= event.timestamp)
                .order_by(OdooPosActivityBucket.period_start.desc()).first())
        pos_age = now - ensure_aware(sale.synced_at) if sale else None
        pos_fresh = bool(pos_age is not None and timedelta(0) <= pos_age <=
                         timedelta(minutes=pos_max_age_minutes))
        if sale and sale.transaction_count > 0 and pos_fresh:
            continue
        status = "pos_unverified" if not pos_fresh else "no_sale_in_window"
        upsert_case(
            db, dedup_key=f"changing-room:{event.id}", case_type="changing_room_review",
            severity="medium", store_id=camera.store_id, camera_id=camera.id,
            zone_id=zone.id, event_id=event.id,
            title="Changing-room activity requires neutral review",
            description=("Review the incident context and available clip. This is a data-correlation "
                         "prompt, not evidence of theft or misconduct."),
            evidence={"pos_status": status, "grace_minutes": grace_minutes,
                      "visual_signal": "changing_room_exit", "human_review_required": True},
        )
        count += 1
    return count
