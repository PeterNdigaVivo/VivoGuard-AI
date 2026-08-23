"""Fail-soft, read-only Odoo synchronisation tasks (feature flagged off)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from celery import shared_task
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.integrations.odoo_client import client_from_settings
from app.models import (
    OdooPosActivityBucket, OdooPosSession, OdooRosterWindow, OdooStoreMap,
    OdooStoreSalesHourly, OdooSyncState,
)
from app.operations.odoo_assurance import (
    compute_conversion_metrics, compute_till_conflicts, create_changing_room_reviews,
    pseudonymise_employee,
)

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_odoo_datetime(value) -> datetime | None:  # type: ignore[no-untyped-def]
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=timezone.utc)


def _m2o_id(value) -> int | None:  # type: ignore[no-untyped-def]
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def _state(db: Session, stream: str) -> OdooSyncState:
    row = db.query(OdooSyncState).filter_by(stream=stream).one_or_none()
    if row is None:
        row = OdooSyncState(stream=stream)
        db.add(row)
        db.flush()
    return row


def _circuit_open(row: OdooSyncState, now: datetime) -> bool:
    until = row.circuit_open_until
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now


def _run_stream(stream: str, operation) -> dict:  # type: ignore[no-untyped-def]
    if not settings.odoo_sync_enabled:
        return {"status": "disabled", "stream": stream}
    now = utc_now()
    with SessionLocal() as db:
        state = _state(db, stream)
        if _circuit_open(state, now):
            return {"status": "circuit_open", "stream": stream,
                    "retry_at": state.circuit_open_until.isoformat()}
        state.last_attempt_at = now
        db.commit()
        try:
            result = operation(db, client_from_settings(settings), state)
            state.last_success_at = utc_now()
            state.consecutive_failures = 0
            state.circuit_open_until = None
            state.last_error = None
            db.commit()
            return {"status": "ok", "stream": stream, **result}
        except Exception as exc:
            db.rollback()
            state = _state(db, stream)
            state.last_attempt_at = now
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
            message = str(exc)
            for secret in (settings.odoo_api_key, settings.odoo_user,
                           settings.odoo_db, settings.odoo_url):
                if secret:
                    message = message.replace(secret, "<redacted>")
            state.last_error = message[:500]
            if state.consecutive_failures >= settings.odoo_circuit_failures:
                state.circuit_open_until = now + timedelta(
                    minutes=settings.odoo_circuit_cooldown_minutes)
            db.commit()
            log.warning("Odoo %s sync failed softly: %s", stream, state.last_error)
            return {"status": "unavailable", "stream": stream}


def _sync_store_master(db: Session, client, state: OdooSyncState) -> dict:  # type: ignore[no-untyped-def]
    domain = []
    if state.cursor_at:
        cursor = state.cursor_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        domain = [("write_date", ">=", cursor)]
    warehouses = client.search_read(
        "stock.warehouse", domain, ["id", "name", "code", "write_date", "company_id"],
    )
    configs = client.search_read(
        "pos.config", [], ["id", "name", "warehouse_id", "write_date", "active"],
    )
    config_by_wh: dict[int, int] = {}
    for cfg in configs:
        warehouse_id = _m2o_id(cfg.get("warehouse_id"))
        if warehouse_id and cfg.get("active", True):
            config_by_wh.setdefault(warehouse_id, int(cfg["id"]))
    updated = 0
    for wh in warehouses:
        mapping = db.query(OdooStoreMap).filter_by(
            odoo_model="stock.warehouse", odoo_res_id=int(wh["id"])).one_or_none()
        if mapping is None:
            continue  # mapping is a governed CSV/admin decision, never guessed
        mapping.name = str(wh["name"])
        mapping.code = str(wh.get("code") or "") or None
        mapping.odoo_pos_config_id = config_by_wh.get(int(wh["id"]))
        mapping.last_synced_at = utc_now()
        mapping.sync_error = None
        updated += 1
    for cfg in configs:
        mapping = db.query(OdooStoreMap).filter_by(
            odoo_model="pos.config", odoo_res_id=int(cfg["id"])).one_or_none()
        if mapping is None:
            continue
        warehouse_id = _m2o_id(cfg.get("warehouse_id"))
        warehouse = next((row for row in warehouses if int(row["id"]) == warehouse_id), None)
        mapping.name = str(cfg["name"])
        mapping.code = str((warehouse or {}).get("code") or "") or mapping.code
        mapping.odoo_pos_config_id = int(cfg["id"])
        mapping.last_synced_at = utc_now()
        mapping.sync_error = None
        updated += 1
    state.cursor_at = utc_now()
    return {"read": len(warehouses) + len(configs), "updated": updated}


@shared_task(name="odoo.sync_store_master")
def sync_store_master() -> dict:
    return _run_stream("store_master", _sync_store_master)


def _calendar_windows(client) -> dict[int, list[dict]]:  # type: ignore[no-untyped-def]
    rows = client.search_read(
        "resource.calendar.attendance", [],
        ["calendar_id", "dayofweek", "hour_from", "hour_to", "date_from", "date_to", "display_type"],
    )
    result: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        cid = _m2o_id(row.get("calendar_id"))
        if cid is not None and not row.get("display_type"):
            result[cid].append(row)
    return result


def _hour_to_time(value: float) -> time:
    hours = int(value)
    minutes = round((float(value) - hours) * 60)
    if minutes == 60:
        hours, minutes = hours + 1, 0
    return time(min(hours, 23), minutes)


def _sync_roster(db: Session, client, state: OdooSyncState) -> dict:  # type: ignore[no-untyped-def]
    employees = client.search_read(
        "hr.employee", [("active", "=", True)],
        ["id", "resource_calendar_id", "work_location_name", "tz"],
    )
    calendars = _calendar_windows(client)
    mappings = db.query(OdooStoreMap).all()
    by_name = {m.name.strip().lower(): m for m in mappings}
    today = utc_now().date()
    created = 0
    for employee in employees:
        location = str(employee.get("work_location_name") or "").strip().lower()
        mapping = by_name.get(location)
        calendar_id = _m2o_id(employee.get("resource_calendar_id"))
        if mapping is None or calendar_id is None:
            continue
        tz = ZoneInfo(mapping.timezone or "Africa/Nairobi")
        employee_ref = pseudonymise_employee(int(employee["id"]), settings.jwt_secret)
        for offset in range(8):
            work_day = today + timedelta(days=offset)
            for attendance in calendars.get(calendar_id, []):
                if int(attendance["dayofweek"]) != work_day.weekday():
                    continue
                start_local = datetime.combine(work_day, _hour_to_time(attendance["hour_from"]), tzinfo=tz)
                end_local = datetime.combine(work_day, _hour_to_time(attendance["hour_to"]), tzinfo=tz)
                row = db.query(OdooRosterWindow).filter_by(
                    store_id=mapping.store_id, work_day=work_day,
                    employee_ref=employee_ref).one_or_none()
                if row is None:
                    row = OdooRosterWindow(store_id=mapping.store_id, work_day=work_day,
                                           employee_ref=employee_ref,
                                           shift_start=start_local.astimezone(timezone.utc),
                                           shift_end=end_local.astimezone(timezone.utc))
                    db.add(row)
                else:
                    proposed_start = start_local.astimezone(timezone.utc)
                    proposed_end = end_local.astimezone(timezone.utc)
                    existing_start = (row.shift_start.replace(tzinfo=timezone.utc)
                                      if row.shift_start.tzinfo is None else row.shift_start)
                    existing_end = (row.shift_end.replace(tzinfo=timezone.utc)
                                    if row.shift_end.tzinfo is None else row.shift_end)
                    row.shift_start = min(existing_start, proposed_start)
                    row.shift_end = max(existing_end, proposed_end)
                    row.synced_at = utc_now()
                created += 1
    cutoff = today - timedelta(days=settings.odoo_roster_retention_days)
    purged = db.query(OdooRosterWindow).filter(OdooRosterWindow.work_day < cutoff).delete()
    state.cursor_at = utc_now()
    return {"employees_considered": len(employees), "windows_upserted": created, "purged": purged}


@shared_task(name="odoo.sync_roster")
def sync_roster() -> dict:
    return _run_stream("roster", _sync_roster)


def _sync_pos_sessions(db: Session, client, state: OdooSyncState) -> dict:  # type: ignore[no-untyped-def]
    cursor = state.cursor_at or (utc_now() - timedelta(days=2))
    rows = client.search_read(
        "pos.session", [("write_date", ">=", cursor.strftime("%Y-%m-%d %H:%M:%S"))],
        ["id", "config_id", "state", "start_at", "stop_at", "write_date"],
    )
    by_config = {m.odoo_pos_config_id: m for m in db.query(OdooStoreMap).filter(
        OdooStoreMap.odoo_pos_config_id.is_not(None)).all()}
    updated = 0
    for item in rows:
        config_id = _m2o_id(item.get("config_id"))
        mapping = by_config.get(config_id)
        if mapping is None or config_id is None:
            continue
        row = db.query(OdooPosSession).filter_by(odoo_session_id=int(item["id"])).one_or_none()
        if row is None:
            row = OdooPosSession(odoo_session_id=int(item["id"]), store_id=mapping.store_id,
                                 odoo_config_id=config_id, state=str(item.get("state") or "unknown"))
            db.add(row)
        row.store_id = mapping.store_id
        row.state = str(item.get("state") or "unknown")
        row.opened_at = parse_odoo_datetime(item.get("start_at"))
        row.closed_at = parse_odoo_datetime(item.get("stop_at"))
        row.synced_at = utc_now()
        updated += 1
    state.cursor_at = utc_now() - timedelta(minutes=2)
    return {"read": len(rows), "updated": updated}


@shared_task(name="odoo.sync_pos_sessions")
def sync_pos_sessions() -> dict:
    result = _run_stream("pos_sessions", _sync_pos_sessions)
    if result.get("status") == "ok":
        with SessionLocal() as db:
            result["conflicts"] = compute_till_conflicts(db, utc_now().date())
            db.commit()
    return result


def _sync_sales(db: Session, client, state: OdooSyncState) -> dict:  # type: ignore[no-untyped-def]
    cursor = state.cursor_at or (utc_now() - timedelta(hours=3))
    changed = client.search_read(
        "pos.order", [("write_date", ">=", cursor.strftime("%Y-%m-%d %H:%M:%S"))],
        ["config_id", "date_order", "write_date"],
    )
    by_config = {m.odoo_pos_config_id: m.store_id for m in db.query(OdooStoreMap).filter(
        OdooStoreMap.odoo_pos_config_id.is_not(None)).all()}
    affected: set[tuple[int, datetime]] = set()
    affected_buckets: set[tuple[int, datetime]] = set()
    hours: set[datetime] = set()
    # One-minute counts are precise enough for the 15-minute changing-room
    # grace window while remaining anonymous store-level aggregates.
    bucket_minutes = 1
    for item in changed:
        store_id = by_config.get(_m2o_id(item.get("config_id")))
        ordered_at = parse_odoo_datetime(item.get("date_order"))
        if store_id is None or ordered_at is None:
            continue
        hour = ordered_at.replace(minute=0, second=0, microsecond=0)
        affected.add((store_id, hour))
        affected_buckets.add((store_id, ordered_at.replace(
            minute=(ordered_at.minute // bucket_minutes) * bucket_minutes,
            second=0, microsecond=0)))
        hours.add(hour)
    # Re-read each affected hour in full. Incrementally adding changed orders
    # double-counts edits and cannot remove cancelled orders. Cap the repair
    # set so a bad cursor cannot create an unbounded Odoo scan.
    selected_hours = sorted(hours, reverse=True)[:48]
    completed: list[dict] = []
    for hour in selected_hours:
        completed.extend(client.search_read(
            "pos.order",
            [("date_order", ">=", hour.strftime("%Y-%m-%d %H:%M:%S")),
             ("date_order", "<", (hour + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
             ("state", "in", ["paid", "done", "invoiced"])],
            ["config_id", "date_order", "amount_total"],
        ))
    hourly: dict[tuple[int, datetime], tuple[int, float]] = {
        key: (0, 0.0) for key in affected if key[1] in selected_hours}
    buckets: dict[tuple[int, datetime], int] = {
        key: 0 for key in affected_buckets if key[1].replace(minute=0) in selected_hours}
    for item in completed:
        store_id = by_config.get(_m2o_id(item.get("config_id")))
        ordered_at = parse_odoo_datetime(item.get("date_order"))
        if store_id is None or ordered_at is None:
            continue
        hour = ordered_at.replace(minute=0, second=0, microsecond=0)
        n, total = hourly.get((store_id, hour), (0, 0.0))
        hourly[(store_id, hour)] = (n + 1, total + float(item.get("amount_total") or 0))
        bucket = ordered_at.replace(
            minute=(ordered_at.minute // bucket_minutes) * bucket_minutes,
            second=0, microsecond=0)
        buckets[(store_id, bucket)] = buckets.get((store_id, bucket), 0) + 1
    for (store_id, hour), (n, total) in hourly.items():
        row = db.query(OdooStoreSalesHourly).filter_by(
            store_id=store_id, period_start=hour).one_or_none()
        if row is None:
            row = OdooStoreSalesHourly(store_id=store_id, period_start=hour)
            db.add(row)
        row.transaction_count = n
        row.amount_total = total
        row.synced_at = utc_now()
    for (store_id, bucket), n in buckets.items():
        row = db.query(OdooPosActivityBucket).filter_by(
            store_id=store_id, period_start=bucket).one_or_none()
        if row is None:
            row = OdooPosActivityBucket(store_id=store_id, period_start=bucket)
            db.add(row)
        row.transaction_count = n
        row.synced_at = utc_now()
    state.cursor_at = utc_now() - timedelta(minutes=2)
    return {"orders_changed": len(changed), "orders_reconciled": len(completed),
            "hours_upserted": len(hourly), "activity_buckets_upserted": len(buckets),
            "hours_skipped_by_bound": max(0, len(hours) - len(selected_hours))}


@shared_task(name="odoo.sync_sales_and_assurance")
def sync_sales_and_assurance() -> dict:
    result = _run_stream("sales", _sync_sales)
    if result.get("status") == "ok":
        with SessionLocal() as db:
            hour = (utc_now() - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            result["conversion_rows"] = compute_conversion_metrics(
                db, hour, maximum=settings.odoo_conversion_max)
            result["changing_room_reviews"] = create_changing_room_reviews(
                db, utc_now(), grace_minutes=settings.odoo_changing_room_grace_minutes,
                pos_max_age_minutes=max(30, settings.odoo_txn_minutes * 2))
            db.commit()
    return result
