# Reducing "Checkout Taking Too Long" false alerts

Checkout-dwell alerts fire when a person stays in a `counter` zone longer
than `checkout_alert_minutes` (default **8 min**). The #1 cause of false
alerts is **staff** working the till being timed like a stuck customer.
This runbook is how to fix that per store.

## 1. Draw a `staff_zone` polygon behind every counter (most important)
- In the camera's zone editor, draw a polygon over the **staff side** of
  the till (where staff stand/work — behind the counter), and tag it
  **`staff_zone`** (or `staff_area`).
- Keep it on the staff side only. Customers stand on the **customer
  side** at the `counter` zone — do **not** let the `staff_zone` overlap
  where customers queue/pay.
- Every till gets its own `staff_zone`. This is the single highest-impact
  step — without it the system cannot reliably tell staff from a stuck
  customer on a plain counter camera.

## 2. How the staff filter works
- The checkout timer **skips a person entirely** (no session, no alert)
  when they are a **reliable** staff member:
  - **Inside a `staff_zone`/`staff_area` polygon** → treated as staff,
    never timed. ← this is why step 1 matters.
  - **Uniform + visible lanyard** (HIGH confidence) → treated as staff.
- It **deliberately still times** a person who only looks like staff by
  "dark clothes + long dwell" at a plain counter — because that is also
  exactly how a **dark-clothed customer stuck at the till** looks, and we
  must alert on them. Uniform-colour-alone is not enough to suppress.
- Other guards already in place:
  - Sessions **over 15 min** are dropped as "staff working the shift".
  - **One checkout alert per store per 30 min** (deduped per store).
  - Alerts never fire **outside store business hours**.

## 3. When a store still gets checkout false alerts
Work down this list:
- [ ] Is a `staff_zone` polygon drawn behind that till? (usual cause — do step 1)
- [ ] Does the `staff_zone` actually cover where staff stand? Re-check the
      polygon against a live frame; nudge it if staff stand outside it.
- [ ] Are staff wearing lanyards visibly? A visible lanyard also suppresses.
- [ ] Is the `counter` zone too big (spilling into the staff area or the
      aisle)? Tighten it to just the customer-facing till point.
- [ ] Still noisy on a genuinely busy till? Raise `checkout_alert_minutes`
      (e.g. 8 → 12) in `.env` and recreate the workers — fewer short
      sessions qualify.
- [ ] Confirm the fix is deployed: workers rebuilt after the latest pull
      (`docker compose up -d --build worker-inference worker-alerts`).

## 4. Per-store checklist (Peter)
For each store, once:
- [ ] Every till camera has a `counter` zone on the **customer** side.
- [ ] Every till camera has a `staff_zone` polygon on the **staff** side.
- [ ] `counter` and `staff_zone` do **not** overlap.
- [ ] Spot-check the Alerts page for a day: checkout alerts should be for
      real customers, not staff at the till.
- [ ] If a specific till still misfires, run the step-3 list for it.

_Related config (`.env`): `checkout_alert_minutes` (threshold),
`checkout_max_dwell_seconds` (staff-shift cutoff, 900s). Dedup is fixed at
one alert per store / 30 min._
