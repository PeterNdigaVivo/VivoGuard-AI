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

## Current limits (so expectations are right)

- Counter visits are only logged for cameras whose zones carry the
  `counter` tag — stores without a counter zone always answer
  `unmatched`. Draw counter zones first.
- Verdicts are recorded for audit/reporting; unmatched sales do **not**
  yet raise an operator alert. Turning "sale with no customer present"
  into an alert is a deliberate follow-up decision (volume/fraud-policy
  dependent), not a wiring gap.
