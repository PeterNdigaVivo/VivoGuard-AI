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


async def _verify_odoo_signature(request: Request) -> bytes:
    """HMAC check. Returns the raw body for downstream parsing."""
    secret = str(getattr(settings, "odoo_webhook_secret", "") or "")
    if not secret:
        # Fail closed: an unsigned integration endpoint must not exist.
        raise HTTPException(503, "odoo webhook not configured")
    ts = request.headers.get("X-Odoo-Timestamp", "")
    sig = request.headers.get("X-Odoo-Signature", "")
    if not ts or not sig:
        raise HTTPException(401, "missing signature headers")
    try:
        age = abs(time.time() - float(ts))
    except ValueError:
        raise HTTPException(400, "bad timestamp header")
    max_age = int(getattr(settings, "odoo_webhook_max_age_seconds", 300))
    if age > max_age:
        raise HTTPException(400, "stale webhook timestamp")
    body = await request.body()
    expected = hmac.new(secret.encode(),
                        f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.strip().lower()):
        raise HTTPException(401, "bad signature")
    return body


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

    store = (db.query(Store)
               .filter(Store.code == payload.store_code).first())
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
    log.info("odoo sale-confirmed: store=%s order=%s -> %s (visit=%s)",
             store.id, payload.order_id, row.status,
             row.zone_visit_id)
    if visit is not None:
        return {"status": "validated", "visit_id": visit.id}
    return {"status": "unmatched", "reason": "no_customer_presence"}
