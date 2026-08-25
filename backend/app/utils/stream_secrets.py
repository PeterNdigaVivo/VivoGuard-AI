"""Redact camera credentials before text reaches logs, Redis, or APIs."""
from __future__ import annotations

import re
from typing import Any


_URL_USERINFO = re.compile(
    r"(?P<scheme>\b(?:rtsp|rtsps|http|https)://)[^/@\s]+@",
    re.IGNORECASE,
)
_QUERY_CREDENTIAL = re.compile(
    r"(?P<prefix>[?&](?:password|passwd|pwd|username|user|token)=)[^&\s]+",
    re.IGNORECASE,
)


def redact_stream_credentials(value: str | None) -> str:
    """Return diagnostic text with URL userinfo and credential params hidden."""
    text = "" if value is None else str(value)
    text = _URL_USERINFO.sub(r"\g<scheme>****:****@", text)
    return _QUERY_CREDENTIAL.sub(r"\g<prefix>****", text)


def strip_stream_userinfo(value: str | None) -> str:
    """Remove URL userinfo when authentication is supplied separately."""
    text = "" if value is None else str(value)
    return _URL_USERINFO.sub(r"\g<scheme>", text)


def redact_stream_structure(value: Any) -> Any:
    """Recursively redact diagnostic structures before they become observable."""
    if isinstance(value, str):
        return redact_stream_credentials(value)
    if isinstance(value, dict):
        return {key: redact_stream_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_stream_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_stream_structure(item) for item in value)
    return value
