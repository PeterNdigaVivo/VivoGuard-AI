"""Automatic transport negotiation.

Lumana-style auto-fallback. When a camera is configured as RTSP but
the store router doesn't forward port 554 (the recurring Moi Ave /
TRM / Acacia situation), we don't want operators to flip a toggle per
camera — the streamer should figure it out and switch the row to
`http_snapshot` automatically.

Algorithm:
  1. Quick TCP probe of (host, rtsp_port). If reachable, transport
     stays 'rtsp' and we exit (cheap path — ~2ms).
  2. RTSP unreachable. Try every common snapshot URL pattern (Dahua
     CGI, Hikvision ISAPI, generic ONVIF) on every common HTTP port.
  3. First (port, URL pattern) that returns a JPEG wins. The
     streamer persists transport='http_snapshot' (+ http_port if
     non-default + snapshot_url_override if non-Dahua) so the next
     poll spawns the right worker with the right URL.

Cache is TTL-based: probes are remembered for 5 minutes so we don't
hammer NVRs on every reconcile, but router port-forward fixes get
picked up without a streamer restart.
"""
from __future__ import annotations
import logging
import socket
import time
from typing import Optional

import httpx

log = logging.getLogger("streamer.auto_transport")


# Probe cache. Key = (host, rtsp_port); value = expiry epoch seconds.
# 5-minute TTL: short enough to detect router fixes within minutes,
# long enough to avoid hammering NVRs on every reconcile.
_PROBE_TTL_SECONDS = 300
_PROBED: dict[tuple[str, int], float] = {}

# Diagnostic record of the LAST probe per camera. Used by the
# /cameras/{id}/transport-diagnose endpoint so operators can see
# exactly what was tried and what each port returned, without
# scrolling through streamer logs.
_LAST_PROBE: dict[tuple[str, int], dict] = {}

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


# Snapshot URL templates. {scheme}, {host}, {port}, {ch} substituted in.
# Multiple per vendor because firmware variants differ. We try every
# pattern on every port — first JPEG wins. Both http:// and https://
# are attempted on every port via the `_SNAPSHOT_SCHEMES` list below.
_SNAPSHOT_TEMPLATES = [
    # Dahua / generic CGI (used by Dahua, Amcrest, Lorex)
    ("dahua-cgi",   "{scheme}://{host}:{port}/cgi-bin/snapshot.cgi?channel={ch}"),
    # Hikvision ISAPI standard. Channel encoded as {N}01 where N is
    # the 1-indexed channel; ISAPI distinguishes mainstream (01) vs
    # substream (02).
    ("hik-isapi",   "{scheme}://{host}:{port}/ISAPI/Streaming/channels/{ch}01/picture"),
    # Older Hikvision firmware without the /ISAPI prefix.
    ("hik-legacy",  "{scheme}://{host}:{port}/Streaming/channels/{ch}01/picture"),
    # Generic ONVIF media snapshot (some cameras expose this directly).
    ("onvif-snap",  "{scheme}://{host}:{port}/onvif/media_service/snapshot?ProfileToken=Profile_{ch}"),
    # Axis / Vivotek style
    ("axis-jpg",    "{scheme}://{host}:{port}/axis-cgi/jpg/image.cgi?camera={ch}"),
]

# Schemes to try, in order. HTTPS goes second because most NVRs in
# the Vivo fleet serve HTTP — checking HTTP first means we usually
# don't pay the TLS handshake cost. Some Hikvision firmwares only
# expose the snapshot endpoint over TLS, hence the fallback.
_SNAPSHOT_SCHEMES = ("http", "https")


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True iff a TCP handshake on host:port completes within
    `timeout` seconds. Cheap — single connect() with hard deadline.

    Both `Connection refused` (host alive, no listener) and `Timed
    out` (router not forwarding the port) return False here — they're
    both "we can't talk RTSP".
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _try_snapshot(url: str, username: str | None, password: str | None,
                  timeout: float = 4.0) -> tuple[bool, str]:
    """One snapshot probe. Returns (success, reason).

    `success` is True iff the response is HTTP 200 AND the body
    starts with the JPEG magic bytes (FFD8FF). `reason` is a short
    human-readable explanation suitable for surfacing in the UI
    diagnostic view.
    """
    try:
        digest = httpx.DigestAuth(username, password or "") if username else None
        with httpx.Client(timeout=timeout, verify=False) as c:
            r = c.get(url, auth=digest)
            if r.status_code == 401 and username:
                r = c.get(url, auth=(username, password or ""))
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        if not r.content:
            return False, "empty response"
        if r.content[:3] != b"\xff\xd8\xff":
            return False, f"not a JPEG (magic={r.content[:4]!r})"
        return True, f"JPEG ok ({len(r.content)} bytes)"
    except httpx.ConnectError as e:
        return False, f"connect failed: {e}"
    except httpx.TimeoutException:
        return False, "timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _probe_port_all_templates(
    host: str, port: int, channel: int,
    username: str | None, password: str | None,
) -> Optional[dict]:
    """Try every snapshot template on this port across both HTTP and
    HTTPS schemes. Return the FIRST combination that yields a JPEG,
    or None.

    Quick TCP check first so we don't waste N × M × 4s on a port
    that's not even open.
    """
    if not _tcp_reachable(host, port, timeout=2.0):
        return None
    for scheme in _SNAPSHOT_SCHEMES:
        for vendor, tmpl in _SNAPSHOT_TEMPLATES:
            url = tmpl.format(scheme=scheme, host=host, port=port,
                              ch=channel or 1)
            ok, reason = _try_snapshot(url, username, password)
            if ok:
                log.info("auto-transport: %s:%s -> JPEG via %s/%s (%s)",
                         host, port, scheme, vendor, reason)
                return {"vendor": vendor, "scheme": scheme,
                        "url": url, "port": port, "reason": reason}
            log.debug("auto-transport: %s:%s/%s/%s -> %s",
                      host, port, scheme, vendor, reason)
    return None


def _record_diagnostic(host: str, rtsp_port: int, record: dict) -> None:
    """Stash the per-camera probe result so the API can show it."""
    record["timestamp"] = time.time()
    _LAST_PROBE[(host, rtsp_port)] = record


def negotiate(camera: dict, password_plain: str) -> Optional[dict]:
    """Decide whether `camera` should be switched to HTTP snapshot.

    Returns a dict of column updates to persist if a switch is needed
    (e.g. {"transport": "http_snapshot", "http_port": 7000,
            "snapshot_url_override": "<hikvision url>"}), or None if
    no change is required.

    Memoised with a 5-minute TTL — cheap on healthy cameras, but
    re-probes automatically when port-forwards get fixed.
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
    now = time.time()
    cached_until = _PROBED.get(key, 0.0)
    if cached_until > now:
        return None

    diagnostic: dict = {
        "host": host, "rtsp_port": rtsp_port,
        "rtsp_reachable": False,
        "http_attempts": [],
        "outcome": None,
    }

    if _tcp_reachable(host, rtsp_port):
        log.info("auto-transport: %s:%s RTSP reachable — keeping rtsp",
                 host, rtsp_port)
        diagnostic["rtsp_reachable"] = True
        diagnostic["outcome"] = "rtsp_ok"
        _record_diagnostic(host, rtsp_port, diagnostic)
        _PROBED[key] = now + _PROBE_TTL_SECONDS
        return None

    log.warning("auto-transport: %s:%s RTSP unreachable — probing HTTP snapshot",
                host, rtsp_port)

    configured = camera.get("http_port") or 0
    candidates: list[int] = []
    if configured:
        candidates.append(configured)
    for p in _DEFAULT_HTTP_PORTS:
        if p not in candidates:
            candidates.append(p)

    username = camera.get("username") or ""
    channel = camera.get("channel_number") or 1

    for port in candidates:
        tcp_ok = _tcp_reachable(host, port, timeout=2.0)
        attempt: dict = {"port": port, "tcp": tcp_ok, "templates": []}
        if not tcp_ok:
            diagnostic["http_attempts"].append(attempt)
            continue
        for scheme in _SNAPSHOT_SCHEMES:
            for vendor, tmpl in _SNAPSHOT_TEMPLATES:
                url = tmpl.format(scheme=scheme, host=host, port=port,
                                  ch=channel)
                ok, reason = _try_snapshot(url, username, password_plain)
                attempt["templates"].append({
                    "vendor": vendor, "scheme": scheme,
                    "url": url, "ok": ok, "reason": reason,
                })
                if ok:
                    log.warning("auto-transport: %s -> switching to http_snapshot "
                                "on port %s via %s/%s", host, port, scheme, vendor)
                    diagnostic["http_attempts"].append(attempt)
                    diagnostic["outcome"] = "switched"
                    diagnostic["switched_to"] = {
                        "port": port, "vendor": vendor,
                        "scheme": scheme, "url": url,
                    }
                    _record_diagnostic(host, rtsp_port, diagnostic)
                    _PROBED[key] = now + _PROBE_TTL_SECONDS
                    updates: dict = {"transport": "http_snapshot"}
                    if port != configured:
                        updates["http_port"] = port
                    # For non-Dahua URLs OR https schemes we need to
                    # tell the worker the exact URL — the worker's
                    # default-construction logic assumes Dahua CGI
                    # over plain HTTP.
                    if vendor != "dahua-cgi" or scheme != "http":
                        updates["snapshot_url_override"] = url
                    return updates
        diagnostic["http_attempts"].append(attempt)

    diagnostic["outcome"] = "no_endpoint_found"
    _record_diagnostic(host, rtsp_port, diagnostic)
    _PROBED[key] = now + _PROBE_TTL_SECONDS
    log.error("auto-transport: %s — RTSP unreachable AND no HTTP snapshot "
              "endpoint responded across %d ports × %d URL templates. "
              "Check store router port-forwarding.",
              host, len(candidates), len(_SNAPSHOT_TEMPLATES))
    return None


def last_diagnostic(host: str, rtsp_port: int) -> dict | None:
    """Return the last probe record for this camera, or None if it
    was never probed in this process. Used by /cameras/{id}/transport-
    diagnose."""
    return _LAST_PROBE.get((host, rtsp_port))


def forget(host: str, rtsp_port: int) -> None:
    """Drop the probe cache for one camera. Used by an admin endpoint
    that lets operators re-trigger negotiation after fixing networking
    (e.g. the store router was reconfigured)."""
    _PROBED.pop((host, rtsp_port), None)
    _LAST_PROBE.pop((host, rtsp_port), None)


# ----- Add Camera wizard: parallel port discovery -----

def discover_ports(host: str, username: str | None, password: str | None,
                   channel: int | None = 1) -> dict:
    """Probe ALL common Dahua/Hik ports for one host and report which
    work. Powers the "Auto-detect ports" button in the Add Camera
    wizard so operators don't have to guess port numbers.

    Returns:
      {
        "host": str,
        "rtsp_port": int | None,
        "http_port": int | None,
        "rtsp_candidates": [int, ...],
        "http_candidates": [int, ...],
        "recommended_transport": "rtsp" | "http_snapshot" | None,
        "snapshot_url": str | None,      # full URL that returned a JPEG
        "snapshot_vendor": str | None,   # which template tag worked
      }

    Probes run in a thread pool so port checks finish in roughly the
    longest single probe (~4s) instead of N× that.
    """
    from concurrent.futures import ThreadPoolExecutor

    rtsp_results: dict[int, bool] = {}
    http_winners: dict[int, dict] = {}    # port -> winning probe record

    def _check_rtsp(p: int) -> tuple[int, bool]:
        return p, _tcp_reachable(host, p, timeout=2.0)

    def _check_http(p: int) -> tuple[int, Optional[dict]]:
        return p, _probe_port_all_templates(host, p, channel or 1,
                                            username, password)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for port, ok in pool.map(_check_rtsp, _DEFAULT_RTSP_PORTS):
            rtsp_results[port] = ok
        for port, hit in pool.map(_check_http, _DEFAULT_HTTP_PORTS):
            if hit:
                http_winners[port] = hit

    rtsp_candidates = [p for p in _DEFAULT_RTSP_PORTS if rtsp_results.get(p)]
    http_candidates = [p for p in _DEFAULT_HTTP_PORTS if http_winners.get(p)]

    rtsp_port = rtsp_candidates[0] if rtsp_candidates else None
    http_port = http_candidates[0] if http_candidates else None
    winner = http_winners.get(http_port) if http_port else None

    recommended: Optional[str]
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
        "snapshot_url": winner["url"] if winner else None,
        "snapshot_vendor": winner["vendor"] if winner else None,
    }
