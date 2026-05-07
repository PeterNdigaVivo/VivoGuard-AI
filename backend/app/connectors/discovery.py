"""ONVIF WS-Discovery — broadcast on the LAN to find cameras.

Falls back to an empty list if the wsdiscovery library isn't present (we
keep that import lazy so the API container can still start without it).
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def discover_onvif(timeout: float = 3.0) -> list[dict]:
    """Returns a list of {endpoint, scopes, types} dicts."""
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery
        from wsdiscovery import QName
    except ImportError:
        log.warning("wsdiscovery not installed — LAN discovery disabled")
        return []

    wsd = WSDiscovery()
    wsd.start()
    try:
        services = wsd.searchServices(
            types=[QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")],
            timeout=timeout,
        )
        out = []
        for svc in services:
            xaddrs = list(svc.getXAddrs())
            scopes = [str(s) for s in svc.getScopes()]
            out.append({
                "endpoint": xaddrs[0] if xaddrs else "",
                "scopes": scopes,
                "types": [str(t) for t in svc.getTypes()],
            })
        return out
    finally:
        wsd.stop()
