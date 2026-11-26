"""External integrations — Odoo sales attribution webhook.

POST /integrations/odoo/sale-confirmed
  Odoo calls this when a POS sale is confirmed. We check whether the
  cameras saw a customer at that store's counter around the sale time
  (zone_visits rows written by the HeatmapDetector presence logger)
  and record the verdict in sales_ai_correlation for auditing. The
  endpoint validates and LOGS — it never blocks or mutates the sale.

Auth: HMAC-SHA256 over "<unix timestamp>.<raw body>" with
settings.odoo_webhook_secret, sent as X-Odoo-Signature (hex) +
X-Odoo-Timestamp. Fail-closed: no secret configured -> 503; bad or
stale signature -> 401/400. No JWT — Odoo is a server, not a user.

Note on paths: the spec named /api/v1/integrations/...; this API has
no /v1 segment anywhere (nginx serves every router under /api/...),
so the real path is /api/integrations/odoo/sale-confirmed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import SalesAICorrelation, Store, ZoneVisit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/odoo", tags=["integrations"])

EAT = ZoneInfo("Africa/Nairobi")

# Match window around the sale moment (spec): the visit must have
# started no later than 1 minute AFTER the sale, and must not have
# ended more than 10 minutes BEFORE it. Open visits (customer still at
# the counter) match too.
MATCH_ENTER_SLACK = timedelta(minutes=1)
MATCH_EXIT_LOOKBACK = timedelta(minutes=10)


class SaleConfirmedIn(BaseModel):
    store_code: str
    order_id: int
    timestamp: datetime
    amount_total: float


LAST_RECEIVED_KEY = "vg:odoo:last_received"      # iso ts, valid signed call
LAST_REJECTED_KEY = "vg:odoo:last_rejected"      # iso ts + reason
REJECTED_COUNT_KEY = "vg:odoo:rejected_count"    # rolling counter, 7d TTL


def _mark(key: str, value: str) -> None:
    """Best-effort Redis breadcrumb for the health page; never raises."""
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.set(key, value, ex=7 * 24 * 3600)
    except Exception:
        pass


def _mark_rejected(reason: str) -> None:
    """A webhook call arrived but was refused — make it VISIBLE (health
    breadcrumb + log line) so misconfigured Odoo isn't a silent 401."""
    log.warning("odoo webhook rejected: %s", reason)
    now_iso = datetime.now(timezone.utc).isoformat()
    _mark(LAST_REJECTED_KEY, f"{now_iso} {reason}")
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.incr(REJECTED_COUNT_KEY)
        r.expire(REJECTED_COUNT_KEY, 7 * 24 * 3600)
    except Exception:
        pass


async def _verify_odoo_signature(request: Request) -> bytes:
    """HMAC check. Returns the raw body for downstream parsing."""
    secret = str(getattr(settings, "odoo_webhook_secret", "") or "")
    if not secret:
        # Fail closed: an unsigned integration endpoint must not exist.
        raise HTTPException(503, "odoo webhook not configured")
    ts = request.headers.get("X-Odoo-Timestamp", "")
    sig = request.headers.get("X-Odoo-Signature", "")
    if not ts or not sig:
        _mark_rejected("missing signature headers")
        raise HTTPException(401, "missing signature headers")
    try:
        age = abs(time.time() - float(ts))
    except ValueError:
        _mark_rejected("bad timestamp header")
        raise HTTPException(400, "bad timestamp header")
    max_age = int(getattr(settings, "odoo_webhook_max_age_seconds", 300))
    if age > max_age:
        _mark_rejected(f"stale timestamp ({int(age)}s old)")
        raise HTTPException(400, "stale webhook timestamp")
    body = await request.body()
    expected = hmac.new(secret.encode(),
                        f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.strip().lower()):
        _mark_rejected("bad signature")
        raise HTTPException(401, "bad signature")
    _mark(LAST_RECEIVED_KEY, datetime.now(timezone.utc).isoformat())
    return body


def _resolve_store(db: Session, store_code: str):
    """Store resolution, two ways: VivoGuard's own Store.code first,
    then the Odoo sync's odoo_store_map (matches its `code`, or its
    odoo_pos_config_id / odoo_res_id when Odoo sends a numeric id).
    Returns None when neither matches."""
    store = db.query(Store).filter(Store.code == store_code).first()
    if store is not None:
        return store
    try:
        from app.models import OdooStoreMap
        q = db.query(OdooStoreMap).filter(OdooStoreMap.code == store_code)
        m = q.first()
        if m is None and store_code.isdigit():
            num = int(store_code)
            m = (db.query(OdooStoreMap)
                   .filter((OdooStoreMap.odoo_pos_config_id == num)
                           | (OdooStoreMap.odoo_res_id == num))
                   .first())
        if m is not None:
            return db.get(Store, m.store_id)
    except Exception:
        # The map table may not exist on older schemas; Store.code
        # remains the primary path.
        db.rollback()
    return None


def _counter_camera_and_zone(db: Session, store_id: int):
    """The store's counter evidence anchor: (camera_id, zone_id) of the
    first counter-tagged zone, or (None, None) when the store has no
    counter zone drawn yet."""
    from app.models import Camera, Zone
    row = (db.query(Zone.camera_id, Zone.id)
             .join(Camera, Zone.camera_id == Camera.id)
             .filter(Camera.store_id == store_id)
             .filter(Zone.suppressed.is_(False))
             .order_by(Zone.id.asc())
             .all())
    for cam_id, zone_id in row:
        z = db.get(Zone, zone_id)
        if z is not None and "counter" in (z.detection_types_json or []):
            return int(cam_id), int(zone_id)
    return None, None


def _maybe_alert_unmatched_sale(db: Session, store, payload,
                                order_ts: datetime) -> bool:
    """Ghost-sale alert: a confirmed POS sale with NO camera evidence of
    a customer at the counter. Guarded so it never spams and NEVER
    fails the webhook: requires the flag, a counter-tagged zone in the
    store, and the store open at sale time. Returns whether an alert
    was created."""
    if not bool(getattr(settings, "odoo_unmatched_sale_alerts", True)):
        return False
    try:
        cam_id, zone_id = _counter_camera_and_zone(db, store.id)
        if cam_id is None:
            return False      # no counter zone: unmatched is expected noise
        from app.utils.business_hours import is_open_with_default
        local = order_ts.astimezone(
            ZoneInfo(getattr(store, "timezone", None) or "Africa/Nairobi"))
        if not is_open_with_default(
                getattr(store, "business_hours_json", None), local):
            return False      # closed-hours sales are a different problem
        amount = (f"KES {payload.amount_total:,.0f}"
                  if payload.amount_total is not None else "unknown amount")
        from app.tasks.alerting import _create_info_alert
        _create_info_alert(
            db,
            camera_id=cam_id,
            zone_id=zone_id,
            store_id=store.id,
            detection_type="sales_attribution",
            cls="sale_without_presence",
            extra={
                "priority": "high",
                "title": "Sale with no customer at the counter",
                "message": (f"Odoo confirmed sale {payload.order_id} "
                            f"({amount}) at {store.name} at "
                            f"{local.strftime('%H:%M')}, but the counter "
                            "camera saw no customer around that time."),
                "order_id": payload.order_id,
                "store_code": payload.store_code,
                "amount_total": payload.amount_total,
                "sale_time_local": local.isoformat(),
                "what_to_do": [
                    "Review the attached counter snapshot and recordings "
                    "around the sale time",
                    "Check the POS session/cashier for this order in Odoo",
                    "Confirm the counter zone still covers the till area",
                ],
            },
        )
        db.commit()
        return True
    except Exception:
        log.exception("unmatched-sale alert failed (webhook unaffected): "
                      "store=%s order=%s", store.id, payload.order_id)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _as_utc(ts: datetime) -> datetime:
    """Normalise the sale timestamp. Odoo POS clocks are store-local,
    so a NAIVE timestamp is interpreted as EAT (Africa/Nairobi) — the
    whole Vivo fleet — then converted to UTC for the DB comparison.
    Aware timestamps are converted as-is."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=EAT)
    return ts.astimezone(timezone.utc)


@router.post("/sale-confirmed")
async def sale_confirmed(request: Request,
                         db: Session = Depends(get_db)):
    body = await _verify_odoo_signature(request)
    try:
        payload = SaleConfirmedIn.model_validate_json(body)
    except Exception as e:
        raise HTTPException(422, f"bad payload: {e}")

    store = _resolve_store(db, payload.store_code)
    if store is None:
        # Still auditable — record the miss, then 404.
        db.add(SalesAICorrelation(
            store_id=None, order_id=payload.order_id,
            order_timestamp=_as_utc(payload.timestamp),
            amount_total=payload.amount_total,
            status="unmatched", reason="unknown_store_code"))
        db.commit()
        raise HTTPException(404,
                            f"unknown store_code {payload.store_code!r}")

    order_ts = _as_utc(payload.timestamp)
    # zone_visits rows only exist for counter-tagged zones, so a
    # store+time filter IS the counter-zone filter.
    visit = (db.query(ZoneVisit)
               .filter(ZoneVisit.store_id == store.id,
                       ZoneVisit.entered_at <= order_ts + MATCH_ENTER_SLACK,
                       (ZoneVisit.exited_at.is_(None))
                       | (ZoneVisit.exited_at
                          >= order_ts - MATCH_EXIT_LOOKBACK))
               .order_by(ZoneVisit.entered_at.desc())
               .first())

    row = SalesAICorrelation(
        store_id=store.id, order_id=payload.order_id,
        order_timestamp=order_ts, amount_total=payload.amount_total,
        status="validated" if visit is not None else "unmatched",
        zone_visit_id=visit.id if visit is not None else None,
        reason=None if visit is not None else "no_customer_presence")
    db.add(row)
    db.commit()
    alerted = False
    if visit is None:
        alerted = _maybe_alert_unmatched_sale(db, store, payload, order_ts)
    log.info("odoo sale-confirmed: store=%s order=%s -> %s (visit=%s, "
             "alert=%s)", store.id, payload.order_id, row.status,
             row.zone_visit_id, alerted)
    if visit is not None:
        return {"status": "validated", "visit_id": visit.id}
    return {"status": "unmatched", "reason": "no_customer_presence",
            "alert_created": alerted}
