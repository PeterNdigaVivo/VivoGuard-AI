"""Strict normalisation contract for Odoo POS/inventory webhooks."""
from __future__ import annotations

from datetime import datetime

ODOO_TYPE_MAP = {
    "pos.refund": "refund", "pos.void": "void", "pos.discount": "discount",
    "pos.no_sale": "no_sale", "pos.high_value": "high_value_transaction",
    "stock.picking.expected": "delivery_expected",
    "stock.picking.done": "delivery_received",
    "stock.move": "stock_move", "stock.exit": "stock_exit",
}


def normalise_odoo_event(payload: dict) -> dict:
    """Map an Odoo webhook to VivoGuard's minimal, non-PII event contract."""
    event_name = str(payload.get("event") or "")
    event_type = ODOO_TYPE_MAP.get(event_name)
    if not event_type:
        raise ValueError(f"unsupported Odoo event: {event_name}")
    source_id = payload.get("id") or payload.get("event_id")
    store_id = payload.get("store_id")
    occurred_at = payload.get("occurred_at") or payload.get("write_date")
    if not source_id or not store_id or not occurred_at:
        raise ValueError("Odoo event requires id, store_id and occurred_at")
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    return {"source": "odoo", "source_event_id": str(source_id),
            "store_id": int(store_id), "event_type": event_type,
            "occurred_at": occurred_at, "amount": payload.get("amount_total"),
            "currency": payload.get("currency"), "actor_ref": payload.get("employee_id"),
            "transaction_ref": payload.get("order_ref") or payload.get("picking_ref"),
            "payload": {"odoo_model": payload.get("model"), "company_id": payload.get("company_id")}}
