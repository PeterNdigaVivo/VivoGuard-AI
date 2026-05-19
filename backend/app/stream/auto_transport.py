"""Automatic transport negotiation.

Lumana-style auto-fallback. When a camera is configured as RTSP but
the store router doesn't forward port 554 (the recurring Moi Ave /
TRM / Acacia situation), we don't want operators to flip a toggle per
camera — the streamer should figure it out and switch the row to
`http_snapshot` automatically.

Algorithm:
  1. Quick TCP probe of (host, rtsp_port). If reachable, transport
     stays 'rtsp' and we exit (cheap path — ~2ms).
  2. RTSP unreachable. Try Dahua's snapshot.cgi on a small set of
     common HTTP ports — the one configured on the row first, then
     known-good defaults (80, 7000, 8080).
  3. First port that returns a JPEG wins. The streamer persists
     transport='http_snapshot' + http_port=<that port> to the DB so
     the next poll spawns the right worker.

Cached in memory per process so we don't re-probe healthy cameras on
every reconcile.
"""
from __future__ import annotations
import logging
import socket
from typing import Optional

import httpx

log = logging.getLogger("streamer.auto_transport")


# Probed once per (host, rtsp_port) tuple per process lifetime — the
# streamer will re-probe on container restart, but not every 5s.
_PROBED: set[tuple[str, int]] = set()

# HTTP ports we'll try, in order, when negotiating fallback. Operator-
# configured http_port is added at the front so a value entered in the
# UI beats defaults. Set covers every commonly-deployed Dahua/Hik HTTP
# admin port we've actually seen in the Vivo fleet:
#   80    — default for most consumer-grade cameras
#   8080  — Hikvision DDNS often, Dahua proxied
#   8000  — Hikvision SDK / ISAPI (sometimes also CGI)
#   800   — uncommon Dahua DDNS overlay
#   7000  — Vivo's Dahua NVRs (Moi Ave / Acacia firmware variant)
_DEFAULT_HTTP_PORTS = (80, 8080, 8000, 800, 7000)

# RTSP ports tried by the smart-port auto-detect (`/cameras/
# intelligent-probe`). 554 is the IANA default; the rest are deployments
# we've seen in the wild. Probe order = preference order.
_DEFAULT_RTSP_PORTS = (554, 10554, 5544, 8554)


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True iff a TCP handshake on host:port completes within
    `timeout` seconds. Cheap — single connect() with hard deadline."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _snapshot_jpeg(host: str, port: int, channel: int | None,
                   username: str | None, password: str | None,
                   timeout: float = 4.0) -> bool:
    """Probe the Dahua/generic snapshot CGI on this port. Returns True
    iff the response is HTTP 200 AND the body starts with the JPEG
    magic (FFD8FF). We don't accept HTML error pages or empty bodies.

    Tries HTTP Digest first (Dahua/Hik default); falls back to Basic
    on 401 to cover older firmwares.
    """
    ch = channel or 1
    url = f"http://{host}:{port}/cgi-bin/snapshot.cgi?channel={ch}"
    try:
        digest = httpx.DigestAuth(username, password or "") if username else None
        with httpx.Client(timeout=timeout, verify=False) as c:
            r = c.get(url, auth=digest)
            if r.status_code == 401 and username:
                r = c.get(url, auth=(username, password or ""))
        if r.status_code != 200:
            log.info("snapshot probe %s -> HTTP %s", url, r.status_code)
            return False
        if not r.content or r.content[:3] != b"\xff\xd8\xff":
            log.info("snapshot probe %s -> not a JPEG (got %s bytes, magic %r)",
                     url, len(r.content), r.content[:4])
            return False
        return True
    except Exception as e:
        log.info("snapshot probe %s failed: %s", url, e)
        return False


def negotiate(camera: dict, password_plain: str) -> Optional[dict]:
    """Decide whether `camera` should be switched to HTTP snapshot.

    Returns a dict of column updates to persist if a switch is needed
    (e.g. {"transport": "http_snapshot", "http_port": 7000}), or
    None if no change is required.

    Idempotent and memoised — calling twice for the same camera with
    a reachable RTSP port is essentially free.
    """
    host = camera.get("host")
    rtsp_port = camera.get("rtsp_port") or 554
    transport = (camera.get("transport") or "rtsp") or "rtsp"

    if not host:
        return None
    if transport != "rtsp":
        # Operator has already chosen http_snapshot — respect that
        # and don't auto-revert. (Operator may have picked it for a
        # reason we can't see from here.)
        return None

    key = (host, rtsp_port)
    if key in _PROBED:
        return None
    _PROBED.add(key)

    if _tcp_reachable(host, rtsp_port):
        log.info("auto-transport: %s:%s RTSP reachable — keeping rtsp",
                 host, rtsp_port)
        return None

    log.warning("auto-transport: %s:%s RTSP unreachable — probing HTTP snapshot",
                host, rtsp_port)

    # Build candidate port list: configured first, then defaults
    # (without dups). Operators who set http_port=7000 in the UI
    # get that tried first.
    configured = camera.get("http_port") or 0
    candidates: list[int] = []
    if configured:
        candidates.append(configured)
    for p in _DEFAULT_HTTP_PORTS:
        if p not in candidates:
            candidates.append(p)

    username = camera.get("username") or ""
    channel = camera.get("channel_number")

    for port in candidates:
        if not _tcp_reachable(host, port, timeout=2.0):
            log.info("auto-transport: %s:%s TCP unreachable, skipping",
                     host, port)
            continue
        if _snapshot_jpeg(host, port, channel, username, password_plain):
            log.warning("auto-transport: %s -> switching to http_snapshot on port %s",
                        host, port)
            updates: dict = {"transport": "http_snapshot"}
            if port != configured:
                updates["http_port"] = port
            return updates

    log.error("auto-transport: %s — RTSP unreachable AND no HTTP snapshot "
              "port responded. Check store router port-forwarding.", host)
    return None


def forget(host: str, rtsp_port: int) -> None:
    """Drop the probe cache for one camera. Used by an admin endpoint
    that lets operators re-trigger negotiation after fixing networking
    (e.g. the store router was reconfigured)."""
    _PROBED.discard((host, rtsp_port))


# ----- Add Camera wizard: parallel port discovery -----

def discover_ports(host: str, username: str | None, password: str | None,
                   channel: int | None = 1) -> dict:
    """Probe ALL common Dahua/Hik ports for one host and report which
    work. Powers the "Auto-detect ports" button in the Add Camera
    wizard so operators don't have to guess port numbers.

    Returns:
      {
        "host": str,
        "rtsp_port": int | None,     # first reachable RTSP port
        "http_port": int | None,     # first port that returned a JPEG via snapshot.cgi
        "rtsp_candidates": [int, ...],   # all reachable RTSP ports
        "http_candidates": [int, ...],   # all HTTP ports that handed back a JPEG
        "recommended_transport": "rtsp" | "http_snapshot" | None,
      }

    Probes run in a thread pool so 9 port checks finish in roughly
    the longest single probe (~3s) instead of 9× that.
    """
    from concurrent.futures import ThreadPoolExecutor

    # RTSP: just TCP reachability.
    rtsp_results: dict[int, bool] = {}
    # HTTP: TCP-reachable AND snapshot.cgi returns a JPEG.
    http_results: dict[int, bool] = {}

    def _check_rtsp(p: int) -> tuple[int, bool]:
        return p, _tcp_reachable(host, p, timeout=2.0)

    def _check_http(p: int) -> tuple[int, bool]:
        if not _tcp_reachable(host, p, timeout=2.0):
            return p, False
        return p, _snapshot_jpeg(host, p, channel, username, password, timeout=4.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for port, ok in pool.map(_check_rtsp, _DEFAULT_RTSP_PORTS):
            rtsp_results[port] = ok
        for port, ok in pool.map(_check_http, _DEFAULT_HTTP_PORTS):
            http_results[port] = ok

    rtsp_candidates = [p for p in _DEFAULT_RTSP_PORTS if rtsp_results.get(p)]
    http_candidates = [p for p in _DEFAULT_HTTP_PORTS if http_results.get(p)]

    rtsp_port = rtsp_candidates[0] if rtsp_candidates else None
    http_port = http_candidates[0] if http_candidates else None

    if rtsp_port:
        recommended = "rtsp"
    elif http_port:
        recommended = "http_snapshot"
    else:
        recommended = None

    return {
        "host": host,
        "rtsp_port": rtsp_port,
        "http_port": http_port,
        "rtsp_candidates": rtsp_candidates,
        "http_candidates": http_candidates,
        "recommended_transport": recommended,
    }
