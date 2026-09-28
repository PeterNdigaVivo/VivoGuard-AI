"""Collects Content-Security-Policy violation reports.

nginx sends the CSP in REPORT-ONLY mode (deploy/nginx/templates/*.conf):
browsers block nothing, they just POST here what the policy WOULD have
blocked. The log lines show whether the policy is safe to enforce.

Unauthenticated by necessity (browsers send reports without credentials),
so it is defensive: bodies over 8 KB are refused unread, logged values are
truncated and stripped of newlines, and each distinct violation is logged
at most once an hour per API process, so junk posts cannot flood the log.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request, Response

log = logging.getLogger(__name__)

router = APIRouter(tags=["security"])

MAX_BODY_BYTES = 8 * 1024
DEDUPE_SECONDS = 3600.0
_MAX_TRACKED = 500
_last_logged: dict[tuple[str, str], float] = {}


def _clean(value: object, limit: int = 200) -> str:
    text = str(value or "-")
    return " ".join(text.split())[:limit]


def _violations(payload: object) -> list[tuple[str, str, str]]:
    """(directive, blocked, page) from either report format:
    legacy report-uri  {"csp-report": {...}}
    Reporting API      [{"type": "csp-violation", "body": {...}}]"""
    out: list[tuple[str, str, str]] = []
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        r = payload["csp-report"]
        out.append((r.get("effective-directive") or r.get("violated-directive"),
                    r.get("blocked-uri"), r.get("document-uri")))
    elif isinstance(payload, list):
        for item in payload[:20]:
            body = item.get("body") if isinstance(item, dict) else None
            if isinstance(body, dict):
                out.append((body.get("effectiveDirective"),
                            body.get("blockedURL"), body.get("documentURL")))
    return [(_clean(d, 80), _clean(b), _clean(p)) for d, b, p in out]


def _should_log(key: tuple[str, str]) -> bool:
    now = time.monotonic()
    last = _last_logged.get(key)
    if last is not None and now - last < DEDUPE_SECONDS:
        return False
    if len(_last_logged) >= _MAX_TRACKED:
        _last_logged.clear()          # bounded memory; worst case a re-log
    _last_logged[key] = now
    return True


async def _read_capped(request: Request) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            return None
    return bytes(buf)


@router.post("/csp-report", status_code=204)
async def csp_report(request: Request) -> Response:
    raw = await _read_capped(request)
    if raw is None:
        log.debug("csp-report: body over %d bytes ignored", MAX_BODY_BYTES)
        return Response(status_code=413)
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        log.debug("csp-report: body was not JSON — ignored")
        return Response(status_code=204)
    for directive, blocked, page in _violations(payload):
        if _should_log((directive, blocked)):
            log.info("CSP report-only violation: directive=%s blocked=%s page=%s",
                     directive, blocked, page)
    return Response(status_code=204)
