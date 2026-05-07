"""Network utilities — DDNS resolution, TCP probes, RTSP URL building."""
from __future__ import annotations
import asyncio
import socket
from urllib.parse import quote


def resolve_host(host: str) -> str | None:
    """Resolve `host` to an IPv4 string. Accepts IP or DDNS hostname.
    Returns None if resolution fails (caller decides whether to alert)."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


async def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Cheap reachability check: open TCP, close immediately."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def build_rtsp_url(
    *,
    brand: str,
    host: str,
    port: int = 554,
    username: str | None = None,
    password: str | None = None,
    channel: int | None = None,
    subtype: int = 0,
    override: str | None = None,
) -> str:
    """Construct the canonical RTSP URL for a camera.

    Per-vendor templates:
      Dahua / generic-Dahua:
        rtsp://USER:PASS@HOST:PORT/cam/realmonitor?channel=N&subtype=S
      Hikvision:
        rtsp://USER:PASS@HOST:PORT/Streaming/Channels/N0(S+1)
        (channel=1 → /Streaming/Channels/101 main, /102 sub)
      Generic / ONVIF (caller usually passes override):
        rtsp://USER:PASS@HOST:PORT/  ... (returns override if provided)

    `override` wins over everything when non-empty.
    """
    if override:
        return override

    auth = ""
    if username:
        # urlencode the password — `@`, `:`, `/` all break RTSP parsing.
        u = quote(username, safe="")
        p = quote(password or "", safe="")
        auth = f"{u}:{p}@"

    ch = channel or 1
    if brand in ("dahua",):
        return f"rtsp://{auth}{host}:{port}/cam/realmonitor?channel={ch}&subtype={subtype}"
    if brand in ("hikvision", "hik"):
        # Hik path: channel 1 main = 101, sub = 102.
        path_id = ch * 100 + (subtype + 1)
        return f"rtsp://{auth}{host}:{port}/Streaming/Channels/{path_id}"
    # Generic fallback — unlikely to work without an override.
    return f"rtsp://{auth}{host}:{port}/"
