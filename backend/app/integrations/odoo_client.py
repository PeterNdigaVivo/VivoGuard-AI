"""Bounded, read-only XML-RPC client for Odoo Online."""
from __future__ import annotations

import logging
import socket
import ssl
import time
import xmlrpc.client
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class OdooUnavailable(RuntimeError):
    """Raised after bounded retries; callers must fail soft."""


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):  # type: ignore[no-untyped-def]
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__(context=ssl.create_default_context())
        self.timeout = timeout

    def make_connection(self, host):  # type: ignore[no-untyped-def]
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


@dataclass(frozen=True)
class OdooClientConfig:
    url: str
    db: str
    user: str
    api_key: str
    timeout_seconds: float = 10.0
    page_size: int = 200


class OdooClient:
    """Read methods only. No create/write/unlink/action methods are exposed."""

    def __init__(self, config: OdooClientConfig):
        self.config = config
        base = config.url.rstrip("/")
        transport_cls = _TimeoutSafeTransport if urlsplit(base).scheme == "https" else _TimeoutTransport
        self._common = xmlrpc.client.ServerProxy(
            f"{base}/xmlrpc/2/common", allow_none=True,
            transport=transport_cls(config.timeout_seconds),
        )
        self._object = xmlrpc.client.ServerProxy(
            f"{base}/xmlrpc/2/object", allow_none=True,
            transport=transport_cls(config.timeout_seconds),
        )
        self._uid: int | None = None

    def authenticate(self) -> int:
        if self._uid is not None:
            return self._uid
        uid = self._retry(lambda: self._common.authenticate(
            self.config.db, self.config.user, self.config.api_key, {}))
        if not uid:
            raise OdooUnavailable("Odoo authentication failed")
        self._uid = int(uid)
        return self._uid

    def search_read(
        self, model: str, domain: list[Any], fields: Iterable[str], *,
        order: str = "id", limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Page through a bounded field allowlist.

        `fields` is mandatory so accidental expansion cannot pull customer or
        employee PII when Odoo adds model fields.
        """
        field_list = list(fields)
        if not field_list:
            raise ValueError("Odoo search_read requires an explicit field allowlist")
        uid = self.authenticate()
        # A missing caller limit still has a hard ceiling. This prevents a
        # damaged cursor or unexpectedly broad domain from walking an entire
        # large Odoo table in one task invocation.
        max_rows = limit if limit is not None else self.config.page_size * 25
        page_size = min(max_rows, self.config.page_size)
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < max_rows:
            batch_limit = min(page_size, max_rows - len(rows))
            batch = self._retry(lambda: self._object.execute_kw(
                self.config.db, uid, self.config.api_key,
                model, "search_read", [domain],
                {"fields": field_list, "offset": offset,
                 "limit": batch_limit, "order": order},
            ))
            rows.extend(batch)
            if len(batch) < batch_limit:
                break
            offset += len(batch)
        if len(rows) == max_rows:
            log.warning("Odoo read reached the bounded row limit: model=%s rows=%s",
                        model, max_rows)
        return rows

    def _retry(self, call):  # type: ignore[no-untyped-def]
        last: BaseException | None = None
        for attempt in range(3):
            try:
                return call()
            except (OSError, socket.timeout, xmlrpc.client.Error) as exc:
                last = exc
                if attempt < 2:
                    time.sleep(0.25 * (2 ** attempt))
        message = str(last or "unknown Odoo error")
        if self.config.api_key:
            message = message.replace(self.config.api_key, "<redacted>")
        raise OdooUnavailable(message[:500]) from last


def client_from_settings(settings) -> OdooClient:  # type: ignore[no-untyped-def]
    missing = [name for name in ("odoo_url", "odoo_db", "odoo_user", "odoo_api_key")
               if not str(getattr(settings, name, "") or "").strip()]
    if missing:
        raise OdooUnavailable("Odoo configuration incomplete: " + ", ".join(missing))
    return OdooClient(OdooClientConfig(
        url=settings.odoo_url, db=settings.odoo_db, user=settings.odoo_user,
        api_key=settings.odoo_api_key,
        timeout_seconds=settings.odoo_request_timeout_seconds,
        page_size=settings.odoo_page_size,
    ))
