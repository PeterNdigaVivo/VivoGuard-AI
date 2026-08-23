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
