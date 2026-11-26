# Odoo → VivoGuard sale-confirmed webhook — integration spec for IT

VivoGuard checks every confirmed POS sale against camera evidence of a
customer at that store's counter and records the verdict. Odoo must be
configured (server action / automated action on POS order confirmation)
to POST each sale to VivoGuard as described here.

## Endpoint

```
POST https://vivoops.vivofashiongroup.net/api/integrations/odoo/sale-confirmed
Content-Type: application/json
```

## Authentication — HMAC signature (required)

Shared secret: the value of `ODOO_WEBHOOK_SECRET` in
`/root/VivoGuard-AI/.env` on the VivoGuard server (ask the VivoGuard
admin for it over a private channel; do not email it around).

Each request must carry two headers:

| Header | Value |
|---|---|
| `X-Odoo-Timestamp` | current unix time in seconds, e.g. `1764057600` |
| `X-Odoo-Signature` | hex HMAC-SHA256 of `"<timestamp>.<raw request body>"` using the shared secret |

Python reference (works inside an Odoo server action):

```python
import hmac, hashlib, json, time
body = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body,
               hashlib.sha256).hexdigest()
# send body with headers X-Odoo-Timestamp: ts, X-Odoo-Signature: sig
```

Rules: requests older than 300 s are rejected (keep Odoo's clock on
NTP); the signature is over the EXACT bytes sent — sign the serialized
body, don't re-serialize after signing.

## Payload

```json
{
  "store_code": "NRB-001",
  "order_id": 123456,
  "timestamp": "2026-11-25 14:03:22",
  "amount_total": 4590.00
}
```

- `store_code` — the VivoGuard store code (Stores page). Must match;
  unknown codes are logged and answered 404.
- `timestamp` — sale confirmation time. A naive timestamp is read as
  **EAT (Africa/Nairobi)**; an ISO timestamp with offset is honoured.
- `order_id` — integer POS order id. `amount_total` — optional, KES.

## Responses

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status":"validated","visit_id":N}` | camera evidence of a customer at the counter overlapped the sale |
| 200 | `{"status":"unmatched","reason":"no_customer_presence"}` | sale recorded, no counter presence found — the audit row is the fraud-review signal |
| 400 | stale/invalid timestamp header | fix clock/headers |
| 401 | missing/bad signature | wrong secret or signing bytes |
| 404 | unknown `store_code` | fix the store code mapping |
| 503 | webhook not configured | `ODOO_WEBHOOK_SECRET` unset on VivoGuard |

Every received sale (matched or not) is stored in VivoGuard's
`sales_ai_correlation` table for audit. Matching window: a counter
visit that started no later than 1 min after the sale and ended no
more than 10 min before it (open visits count).

## End-to-end test (run from any machine that can reach the server)

```bash
SECRET='<the shared secret>'
BODY='{"store_code":"NRB-001","order_id":999001,"timestamp":"2026-11-25 14:00:00","amount_total":100.0}'
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Odoo-Timestamp: $TS" \
  -H "X-Odoo-Signature: $SIG" \
  -d "$BODY" \
  https://vivoops.vivofashiongroup.net/api/integrations/odoo/sale-confirmed
```

Expected: `{"status":"validated",...}` or
`{"status":"unmatched","reason":"no_customer_presence"}` — either
proves the wiring. 401 means the secret or signing differs; 404 means
the store code doesn't exist in VivoGuard.

## What happens on the VivoGuard side

- Every received sale (matched or not) is stored in
  `sales_ai_correlation` for audit.
- An **unmatched** sale during trading hours, at a store that has a
  counter-tagged zone, raises a high-priority **"Sale With No Customer
  at Counter"** alert anchored to the counter camera with a snapshot —
  visible under the **💰 Sales** tab on the Alerts page. Kill switch:
  `ODOO_UNMATCHED_SALE_ALERTS=false`.
- **Validated** sales stay audit-only (no alert — hundreds/day of
  "everything is fine" would bury the feed); their count shows on
  Mission Control.
- Mission Control → Integrations → **Odoo sales** light: 🔴 secret
  unset, 🟡 armed/no calls yet, 🟢 sales flowing, with 24h
  validated/unmatched counts. Hovering shows the last valid call's
  timestamp — or, if Odoo is calling with a wrong secret/clock, the
  last **rejection and its reason**, so a misconfiguration is visible
  instead of a silent 401.
- Counter visits are only logged for cameras whose zones carry the
  `counter` tag — stores without a counter zone always answer
  `unmatched`. Draw counter zones first.

## Store mapping (Odoo codes ≠ VivoGuard codes)

`store_code` in the payload resolves in this order:

1. VivoGuard's own `Store.code` (Stores page) — exact match.
2. The Odoo sync's `odoo_store_map` table: its `code` column, or (for a
   numeric `store_code`) its `odoo_pos_config_id` / `odoo_res_id`.

So Odoo may send **its own POS config id or code** — preferred, since it
never drifts. To see which stores are mapped, on the VivoGuard server:

```bash
docker compose exec -T postgres psql -U vivoguard -d vivoguard \
  -c "SELECT s.id, s.name, s.code AS vg_code, m.code AS odoo_code, m.odoo_pos_config_id
      FROM stores s LEFT JOIN odoo_store_map m ON m.store_id = s.id ORDER BY s.id;"
```

Rows with NULL `odoo_code` **and** NULL `vg_code` cannot be attributed —
list those for sign-off and either set `Store.code` in VivoGuard or let
the Odoo sync populate the map.

## Odoo-side configuration (the missing piece)

**Trigger choice: `pos.order` reaching state `paid`/`done`/`invoiced`**,
not `account.move` posting. Rationale: the webhook correlates the sale
with *who was at the counter at that moment*; POS payment is that
moment, while journal posting can lag minutes to hours (batch closes),
which would break the ±minutes matching window.

**Why a small addon, not a plain server action:** Odoo server actions
run in `safe_eval`, which cannot import `hmac`, `hashlib` or
`requests` — the signature cannot be computed there. The supported
route is a ~60-line addon. This also fits governance: a module install
is a reviewable, versioned change.

### Addon: `vivoguard_webhook` (copy-paste, 3 files)

`vivoguard_webhook/__manifest__.py`
```python
{
    "name": "VivoGuard sale-confirmed webhook",
    "version": "1.0.0",
    "depends": ["point_of_sale"],
    "data": [],
    "license": "LGPL-3",
}
```

`vivoguard_webhook/__init__.py`
```python
from . import models
```

`vivoguard_webhook/models/__init__.py`
```python
from . import pos_order
```

`vivoguard_webhook/models/pos_order.py`
```python
import hashlib
import hmac
import json
import logging
import time

import requests

from odoo import models

_logger = logging.getLogger(__name__)

PAID_STATES = ("paid", "done", "invoiced")


class PosOrder(models.Model):
    _inherit = "pos.order"

    def write(self, vals):
        before = {o.id: o.state for o in self}
        res = super().write(vals)
        for order in self:
            if order.state in PAID_STATES and before.get(order.id) not in PAID_STATES:
                order._vivoguard_notify()
        return res

    def _vivoguard_notify(self):
        icp = self.env["ir.config_parameter"].sudo()
        url = icp.get_param("vivoguard.webhook_url")
        secret = icp.get_param("vivoguard.webhook_secret")
        if not url or not secret:
            _logger.warning("VivoGuard webhook not configured; skipping")
            return
        payload = {
            "store_code": str(self.config_id.id),   # resolved via odoo_store_map
            "order_id": self.id,
            "timestamp": (self.date_order or self.create_date).isoformat(),
            "amount_total": float(self.amount_total or 0.0),
        }
        body = json.dumps(payload).encode()
        for attempt in range(3):               # retry with backoff
            ts = str(int(time.time()))
            sig = hmac.new(secret.encode(), ts.encode() + b"." + body,
                           hashlib.sha256).hexdigest()
            try:
                resp = requests.post(
                    url, data=body, timeout=10,
                    headers={"Content-Type": "application/json",
                             "X-Odoo-Timestamp": ts,
                             "X-Odoo-Signature": sig})
                if resp.status_code == 200:
                    return
                _logger.warning("VivoGuard webhook %s: %s %s",
                                self.id, resp.status_code, resp.text[:200])
                if resp.status_code in (401, 404, 422, 503):
                    return                      # config error; retrying won't help
            except Exception as exc:            # noqa: BLE001
                _logger.warning("VivoGuard webhook %s attempt %d: %s",
                                self.id, attempt + 1, exc)
            time.sleep(2 ** attempt)
        _logger.error("VivoGuard webhook gave up for order %s", self.id)
```

### Install steps (route through `vivo_control_governance`)

1. **Maker**: raise the change in `vivo_control_governance` — attach
   this spec and the addon source; change type "new automation on
   pos.order"; blast radius: outbound HTTPS only, no writes to Odoo
   data; rollback: uninstall module.
2. **Checker** approves; then IT places the folder in the Odoo addons
   path and installs "VivoGuard sale-confirmed webhook" from Apps
   (update apps list first).
3. Set the two system parameters (Settings → Technical → Parameters →
   System Parameters):
   - `vivoguard.webhook_url` = `https://vivoops.vivofashiongroup.net/api/integrations/odoo/sale-confirmed`
   - `vivoguard.webhook_secret` = the shared secret (from the VivoGuard
     admin, privately)
4. Pay one test POS order; within seconds the Mission Control "Odoo
   sales" light turns 🟢 (or shows the rejection reason on hover if the
   secret/clock is wrong).

Note the payload's `store_code` is the **POS config id** — VivoGuard
resolves it through `odoo_store_map`, so no per-store code juggling in
the addon.
