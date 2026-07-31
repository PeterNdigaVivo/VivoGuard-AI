"""Network utilities — RTSP URL building."""
from __future__ import annotations
from urllib.parse import quote


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
      Uniview:
        rtsp://USER:PASS@HOST:PORT/media/video1   (channel=1 main)
                              /media/video2   (channel=1 sub)
                              /unicast/c{N}/s{1 or 2}  (alt format)
      Generic / ONVIF (caller usually passes override):
        rtsp://USER:PASS@HOST:PORT/  ... (returns override if provided)

    The URL is the same whether the wire is TCP (-rtsp_transport tcp)
    or HTTP-tunneled (-rtsp_transport http) — the tunnel mode is a
    FFmpeg flag, not a URL change. So this builder just uses whichever
    PORT was provided; pass 80 / 8000 / 7000 when tunneling and 554
    when not, and set the FFmpeg flag separately.

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
    if brand in ("uniview", "univ"):
        # Uniview uses 1-indexed stream IDs; main=1, sub=2.
        stream = subtype + 1
        return f"rtsp://{auth}{host}:{port}/unicast/c{ch}/s{stream}/live"
    # Generic fallback — unlikely to work without an override.
    return f"rtsp://{auth}{host}:{port}/"
