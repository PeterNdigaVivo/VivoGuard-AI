# Odoo integration discovery

VivoGuard's planned Odoo integration is one-way and read-only:

```text
Odoo 18  -- read-only XML-RPC -->  VivoGuard
```

VivoGuard must never create, update, or delete an Odoo record. Phase 0 only
discovers the schema and store identifiers needed to design the integration;
it does not change either production system.

## Create a dedicated read-only Odoo user

Complete these steps in Odoo as an administrator. Menu names can vary when
developer mode or custom Vivo modules are installed.

1. Create a dedicated internal user such as
   `vivoguard-readonly@example.com`. Do not reuse a human administrator or
   finance user's account.
2. Do not grant Administration, Settings, Access Rights, Technical Features,
   import/export administration, or any group that can create, edit, delete,
   post, validate, refund, reconcile, or manage users.
3. Create a narrowly scoped read-only security group and access-control rules
   for only the required models:
   - `res.company`
   - `stock.warehouse`
   - `pos.config`
   - `pos.session`
   - `pos.order`
   - `hr.employee`
   - `resource.calendar`
   - `resource.calendar.attendance`
   - `account.move`
   - the custom store/branch and roster models identified by discovery
4. For every permitted model, allow **Read** only. Leave Create, Write, and
   Delete disabled. Apply company and record rules so the account can see only
   the Vivo companies and stores that VivoGuard is expected to monitor.
5. Do not grant fields containing employee identity, contact, payroll,
   customer, payment, or banking information merely to make discovery pass.
   The discovery utility reads model metadata, record counts, and at most 40
   store code/name pairs; it never reads employee, customer, transaction, or
   monetary records.
6. Generate a dedicated API key for this user. Store it in an approved secrets
   manager and record the owner and rotation date. Do not paste it into source
   code, tickets, chat messages, shell history, or committed files.
7. Verify the boundary with a non-production test record or Odoo access-rights
   inspection: read operations should succeed and create/write/unlink must be
   denied. Do not use the discovery script to test writes; it contains no write
   calls by design.

If Odoo access rules cannot expose `ir.model` or `fields_get` to a restricted
user, an Odoo administrator may run discovery once with a temporary,
time-limited schema-inspection account. Revoke that account immediately after
the JSON is captured. Do not give the long-lived VivoGuard service account
administrative access.

## Run discovery

Requirements:

- Python 3.10 or newer; no third-party Python packages are required.
- Network access to Odoo's HTTPS endpoint.
- The dedicated Odoo database name, login, and API key.

Set environment variables in the process environment. The following PowerShell
example prompts for the API key so it is not written into the command history:

```powershell
$env:ODOO_URL = "https://odoo.example.com"
$env:ODOO_DB = "your_database"
$env:ODOO_USER = "vivoguard-readonly@example.com"
$secureKey = Read-Host "Odoo API key" -AsSecureString
$env:ODOO_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
$env:ODOO_REQUEST_TIMEOUT_SECONDS = "20"
python scripts/odoo_discover.py | Out-File -Encoding utf8 odoo-discovery.json
Remove-Item Env:ODOO_API_KEY
```

On Linux, use a protected temporary environment file rather than putting the
secret directly in a shell command:

```bash
install -m 600 /dev/null /tmp/vivoguard-odoo-discovery.env
${EDITOR:-vi} /tmp/vivoguard-odoo-discovery.env
set -a
. /tmp/vivoguard-odoo-discovery.env
set +a
python3 scripts/odoo_discover.py > odoo-discovery.json
unset ODOO_API_KEY
shred -u /tmp/vivoguard-odoo-discovery.env
```

The temporary file should contain:

```dotenv
ODOO_URL=https://odoo.example.com
ODOO_DB=your_database
ODOO_USER=vivoguard-readonly@example.com
ODOO_API_KEY=use-the-approved-secret-store
ODOO_REQUEST_TIMEOUT_SECONDS=20
```

The script emits exactly one JSON document to standard output. It contains:

- Odoo version and database name;
- existence, count, and field metadata for the requested models;
- candidate custom store, branch, shop, and outlet models;
- no more than 40 store record IDs, codes, and names;
- POS-session usage count for the preceding 30 days; and
- explicit privacy assertions describing data the script does not read.

The command exits non-zero and still emits a safe JSON error document if
configuration, authentication, access, timeout, or transport fails. Errors are
scrubbed of the API key, login, and configured Odoo URL.

## Validate and return the result

1. Confirm the file is valid JSON:

   ```bash
   python3 -m json.tool odoo-discovery.json >/dev/null
   ```

2. Inspect `privacy`, `store_identifiers`, and any `error` fields. The output
   must not contain credentials, employee details, customer data, transactions,
   or monetary values.
3. Transfer `odoo-discovery.json` through the approved secure project channel.
   Although it is deliberately data-minimised, treat store names and schema
   metadata as internal operational information.
4. Keep `ODOO_SYNC_ENABLED=false`. Do not deploy or begin the schema-dependent
   phases until the discovery JSON has been reviewed and the actual Odoo models
   and fields have been confirmed.

## Operational safeguards

- The discovery script invokes only `version`, `authenticate`, `search_read`,
  `search_count`, and `fields_get`.
- Reads are bounded: model metadata is limited to 100 candidate models and
  store samples are limited to 40 total rows.
- It performs no recurring sync and no writes.
- Use HTTPS with a valid certificate. Do not disable TLS verification.
- Revoke or rotate the API key if it appears in terminal output, logs, chat, or
  source control.

## Reviewed production schema (23 August 2026)

The read-only discovery was completed against Odoo 18 Enterprise and confirmed
the required POS, warehouse, session, calendar and employee schema. No customer,
employee, transaction or monetary record was read by the discovery pass.

The important implementation finding is that `stock.warehouse` is **not** a
safe store key on its own: many Kenya POS configurations share the finished-
goods warehouse. `pos.config` is therefore the authoritative till/location key
for store mapping, while its `warehouse_id` is retained only as metadata.

Odoo has company working calendars but no confirmed authoritative retail
trading-hours model linked to each POS location. VivoGuard therefore applies
this precedence:

1. fresh Odoo hours (only when a future authoritative source is mapped);
2. governed manual rows in `store_business_hours`;
3. the existing `stores.business_hours_json`; and
4. the existing fleet-safe default.

Stale or unavailable Odoo data never means “closed” and never suppresses an
alert.

## Phase 1: mapping and business-hours assurance

Generate the mapping template with the dedicated read-only service account:

```bash
PYTHONPATH=backend python scripts/odoo_store_map.py export ops/odoo_store_map.csv
```

Review every `vivoguard_store_name` against the production VivoGuard store
name, remove non-physical configurations (HQ/online), then validate before
applying:

```bash
PYTHONPATH=backend python scripts/odoo_store_map.py apply ops/odoo_store_map.csv --dry-run
PYTHONPATH=backend python scripts/odoo_store_map.py apply ops/odoo_store_map.csv
```

The importer is all-or-nothing when errors exist; it does not guess fuzzy
matches. Run the seven-day classification comparison before enabling sync:

```bash
PYTHONPATH=backend python scripts/odoo_hours_dry_run.py --days 7
```

The `/odoo-assurance` page is restricted by the existing system-admin
allowlist and shows mapping, sync state, open till conflicts, conversion data
quality flags and changing-room review cases.

## Phase 2: roster context

Only `hr.employee.id`, `resource_calendar_id`, `work_location_name` and timezone
are read. VivoGuard stores an HMAC pseudonym, store, date, shift start/end and
sync time for 45 days. It never persists names, contact details, payroll data,
badge/PIN values or facial data. Expected staff is an advisory tag; stale,
missing or positive roster context never suppresses CCTV alerts.

## Phase 3: till conflict reporting

`pos.session` is pulled every 15 minutes using an incremental `write_date`
cursor. Opening and closing signals from both CCTV and POS are retained. A
greater-than-30-minute mismatch, or one missing source, creates a reporting-only
conflict. It does not change detector thresholds or accuse a staff member.

## Phase 4: conversion and changing-room review

VivoGuard reads only the POS configuration, order timestamp, total and state,
then persists hourly store aggregates plus anonymous one-minute transaction
counts for the changing-room grace check. Conversion above 60% is labelled a
data-quality flag because it normally indicates a footfall/POS mismatch.

A configured `changing_room` tripwire may create a neutral human-review case
when an exit signal has no aggregate sale in the grace window. A fresh matching
sale creates no case. Missing or stale POS creates `pos_unverified`; visual
evidence remains available and the wording explicitly states that the signal
is not evidence of theft or misconduct.

## Activation and rollback

`ODOO_SYNC_ENABLED=false` is the default. Before enabling it:

- migrate through `0041`;
- create a least-privilege read-only service user and store its API key in the
  production secret store;
- validate every physical store mapping and timezone;
- run the seven-day hours dry run and controlled store tests; and
- verify the Odoo Assurance page reports successful syncs without circuit
  breaker errors.

Rollback is immediate: set `ODOO_SYNC_ENABLED=false` and restart the beat/worker
services. Existing CCTV inference and alerts continue using their manual/default
context. No Odoo write path exists in this implementation.
