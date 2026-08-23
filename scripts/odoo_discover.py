#!/usr/bin/env python3
"""Discover the minimum Odoo schema needed by VivoGuard.

This script is intentionally standalone and read-only. It uses only Python's
standard library, reads credentials from environment variables, and emits one
JSON document containing schema metadata and store identifiers. It never reads
employee records, customers, transactions, or monetary values.
"""

from __future__ import annotations

import json
import os
import ssl
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REQUIRED_ENV = ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY")
TARGET_MODELS = (
    "res.company",
    "stock.warehouse",
    "pos.config",
    "pos.session",
    "pos.order",
    "hr.employee",
    "resource.calendar",
    "resource.calendar.attendance",
    "account.move",
)
STORE_TERMS = ("store", "branch", "shop", "outlet")
STORE_MODEL_PRIORITY = ("stock.warehouse", "pos.config", "res.company")
STORE_CODE_FIELDS = (
    "code",
    "store_code",
    "branch_code",
    "shop_code",
    "outlet_code",
    "warehouse_code",
    "short_name",
)
POS_DATE_FIELDS = ("start_at", "opening_date", "create_date", "write_date")
MAX_STORE_IDENTIFIERS = 40


class DiscoveryError(RuntimeError):
    """A safe, user-facing discovery failure."""


class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host: str):  # type: ignore[no-untyped-def]
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float) -> None:
        super().__init__(context=ssl.create_default_context())
        self.timeout = timeout

    def make_connection(self, host: str):  # type: ignore[no-untyped-def]
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def load_config(environ: dict[str, str] | os._Environ[str] = os.environ) -> dict[str, Any]:
    missing = [name for name in REQUIRED_ENV if not environ.get(name, "").strip()]
    if missing:
        raise DiscoveryError("Missing required environment variables: " + ", ".join(missing))

    raw_url = environ["ODOO_URL"].strip().rstrip("/")
    parts = urlsplit(raw_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise DiscoveryError("ODOO_URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise DiscoveryError("ODOO_URL must not contain embedded credentials")

    try:
        timeout = float(environ.get("ODOO_REQUEST_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise DiscoveryError("ODOO_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if timeout <= 0 or timeout > 300:
        raise DiscoveryError("ODOO_REQUEST_TIMEOUT_SECONDS must be between 1 and 300")

    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return {
        "url": clean_url,
        "db": environ["ODOO_DB"].strip(),
        "user": environ["ODOO_USER"].strip(),
        "api_key": environ["ODOO_API_KEY"],
        "timeout": timeout,
    }


def make_proxy(url: str, endpoint: str, timeout: float) -> xmlrpc.client.ServerProxy:
    transport: xmlrpc.client.Transport
    if urlsplit(url).scheme == "https":
        transport = TimeoutSafeTransport(timeout)
    else:
        transport = TimeoutTransport(timeout)
    return xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/{endpoint}", transport=transport, allow_none=True
    )


def safe_error(exc: BaseException, secrets: tuple[str, ...]) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:1000]


def or_domain(field_names: tuple[str, ...], terms: tuple[str, ...]) -> list[Any]:
    clauses = [(field, "ilike", term) for field in field_names for term in terms]
    if not clauses:
        return []
    return ["|"] * (len(clauses) - 1) + clauses


class OdooReader:
    def __init__(self, config: dict[str, Any]) -> None:
        self.db = config["db"]
        self.user = config["user"]
        self.api_key = config["api_key"]
        self.common = make_proxy(config["url"], "common", config["timeout"])
        self.object = make_proxy(config["url"], "object", config["timeout"])
        self.uid: int | None = None

    def authenticate(self) -> tuple[dict[str, Any], int]:
        version = self.common.version()
        uid = self.common.authenticate(self.db, self.user, self.api_key, {})
        if not uid:
            raise DiscoveryError("Odoo authentication failed")
        self.uid = int(uid)
        return version, self.uid

    def call(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        if self.uid is None:
            raise DiscoveryError("Odoo client has not authenticated")
        return self.object.execute_kw(
            self.db,
            self.uid,
            self.api_key,
            model,
            method,
            args or [],
            kwargs or {},
        )


def discover_models(reader: OdooReader, secrets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    rows = reader.call(
        "ir.model",
        "search_read",
        [[("model", "in", list(TARGET_MODELS))]],
        {"fields": ["model", "name"], "limit": len(TARGET_MODELS)},
    )
    existing = {row["model"]: row for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for model in TARGET_MODELS:
        if model not in existing:
            result[model] = {"exists": False, "count": None, "fields": []}
            continue
        item: dict[str, Any] = {"exists": True, "count": None, "fields": []}
        try:
            item["count"] = reader.call(model, "search_count", [[]])
        except (TimeoutError, xmlrpc.client.Error, OSError) as exc:
            item["count_error"] = safe_error(exc, secrets)
        try:
            fields = reader.call(
                model,
                "fields_get",
                [],
                {"attributes": ["string", "type"]},
            )
            item["fields"] = [
                {
                    "name": name,
                    "type": metadata.get("type"),
                    "string": metadata.get("string"),
                }
                for name, metadata in sorted(fields.items())
            ]
        except (TimeoutError, xmlrpc.client.Error, OSError) as exc:
            item["fields_error"] = safe_error(exc, secrets)
        result[model] = item
    return result


def discover_custom_store_models(
    reader: OdooReader, secrets: tuple[str, ...]
) -> list[dict[str, Any]]:
    try:
        rows = reader.call(
            "ir.model",
            "search_read",
            [or_domain(("model", "name"), STORE_TERMS)],
            {"fields": ["model", "name"], "limit": 100, "order": "model"},
        )
    except (TimeoutError, xmlrpc.client.Error, OSError) as exc:
        return [{"error": safe_error(exc, secrets)}]
    return [
        {"model": row.get("model"), "name": row.get("name")}
        for row in rows
        if row.get("model") not in TARGET_MODELS
    ]


def field_names(model_info: dict[str, Any]) -> set[str]:
    return {field["name"] for field in model_info.get("fields", [])}


def store_candidates(
    models: dict[str, dict[str, Any]], custom_models: list[dict[str, Any]]
) -> list[str]:
    custom = [row["model"] for row in custom_models if row.get("model")]
    candidates = custom + [
        model for model in STORE_MODEL_PRIORITY if models.get(model, {}).get("exists")
    ]
    return list(dict.fromkeys(candidates))


def discover_store_identifiers(
    reader: OdooReader,
    models: dict[str, dict[str, Any]],
    custom_models: list[dict[str, Any]],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for model in store_candidates(models, custom_models):
        if len(samples) >= MAX_STORE_IDENTIFIERS:
            break
        try:
            if model in models:
                names = field_names(models[model])
            else:
                metadata = reader.call(
                    model, "fields_get", [], {"attributes": ["string", "type"]}
                )
                names = set(metadata)
            code_field = next((field for field in STORE_CODE_FIELDS if field in names), None)
            if "name" not in names:
                checked.append({"model": model, "status": "skipped_no_name_field"})
                continue
            fields = ["name"] + ([code_field] if code_field else [])
            limit = MAX_STORE_IDENTIFIERS - len(samples)
            rows = reader.call(
                model,
                "search_read",
                [[]],
                {"fields": fields, "limit": limit, "order": "id"},
            )
            checked.append(
                {"model": model, "status": "sampled", "code_field": code_field}
            )
            for row in rows:
                samples.append(
                    {
                        "model": model,
                        "res_id": row.get("id"),
                        "code": row.get(code_field) if code_field else None,
                        "name": row.get("name"),
                    }
                )
        except (TimeoutError, xmlrpc.client.Error, OSError) as exc:
            checked.append(
                {"model": model, "status": "error", "error": safe_error(exc, secrets)}
            )
    return {"max_rows": MAX_STORE_IDENTIFIERS, "checked_models": checked, "rows": samples}


def discover_recent_pos_usage(
    reader: OdooReader,
    models: dict[str, dict[str, Any]],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    pos_info = models.get("pos.session", {})
    if not pos_info.get("exists"):
        return {"available": False, "last_30_days_count": None, "date_field": None}
    names = field_names(pos_info)
    date_field = next((field for field in POS_DATE_FIELDS if field in names), None)
    if not date_field:
        return {
            "available": True,
            "last_30_days_count": None,
            "date_field": None,
            "error": "No usable POS session date field was visible",
        }
    since = datetime.now(timezone.utc) - timedelta(days=30)
    try:
        count = reader.call(
            "pos.session",
            "search_count",
            [[(date_field, ">=", since.strftime("%Y-%m-%d %H:%M:%S"))]],
        )
        return {"available": True, "last_30_days_count": count, "date_field": date_field}
    except (TimeoutError, xmlrpc.client.Error, OSError) as exc:
        return {
            "available": True,
            "last_30_days_count": None,
            "date_field": date_field,
            "error": safe_error(exc, secrets),
        }


def run_discovery(reader: OdooReader, config: dict[str, Any]) -> dict[str, Any]:
    secrets = (config["api_key"], config["user"], config["url"])
    version, _uid = reader.authenticate()
    models = discover_models(reader, secrets)
    custom_models = discover_custom_store_models(reader, secrets)
    return {
        "discovery_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "odoo": {
            "database": config["db"],
            "server_version": version.get("server_version"),
            "server_version_info": version.get("server_version_info"),
            "protocol_version": version.get("protocol_version"),
        },
        "models": models,
        "custom_store_models": custom_models,
        "store_identifiers": discover_store_identifiers(
            reader, models, custom_models, secrets
        ),
        "pos_usage": discover_recent_pos_usage(reader, models, secrets),
        "privacy": {
            "store_identifier_records_read": True,
            "transaction_records_read": False,
            "employee_records_read": False,
            "customer_records_read": False,
            "monetary_values_read": False,
        },
    }


def main() -> int:
    try:
        config = load_config()
        result = run_discovery(OdooReader(config), config)
    except (TimeoutError, DiscoveryError, xmlrpc.client.Error, OSError) as exc:
        api_key = os.environ.get("ODOO_API_KEY", "")
        user = os.environ.get("ODOO_USER", "")
        url = os.environ.get("ODOO_URL", "")
        result = {
            "discovery_version": 1,
            "ok": False,
            "error": safe_error(exc, (api_key, user, url)),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    result["ok"] = True
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
