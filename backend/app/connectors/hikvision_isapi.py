"""Hikvision integration via the public ISAPI HTTP interface.

Endpoints used:
  GET  /ISAPI/System/deviceInfo                — device model / firmware
  GET  /ISAPI/ContentMgmt/InputProxy/channels  — channel enumeration (NVR)
  GET  /ISAPI/Streaming/channels/<id>/picture  — JPEG snapshot

ISAPI uses HTTP Digest auth.
"""
from __future__ import annotations
import logging
import xml.etree.ElementTree as ET
import httpx

log = logging.getLogger(__name__)


class HikvisionISAPI:
    def __init__(self, host: str, http_port: int, username: str, password: str, *, ssl: bool = False):
        scheme = "https" if ssl else "http"
        self.base = f"{scheme}://{host}:{http_port}"
        self.auth = httpx.DigestAuth(username, password)

    async def _get(self, path: str, *, timeout: float = 8.0) -> httpx.Response:
        async with httpx.AsyncClient(auth=self.auth, timeout=timeout, verify=False) as c:
            return await c.get(self.base + path)

    async def device_info(self) -> dict:
        r = await self._get("/ISAPI/System/deviceInfo")
        r.raise_for_status()
        ns = "{http://www.hikvision.com/ver20/XMLSchema}"
        root = ET.fromstring(r.text)
        def find(tag: str) -> str:
            el = root.find(f"{ns}{tag}") or root.find(tag)
            return el.text if el is not None and el.text else ""
        return {
            "device_name":   find("deviceName"),
            "model":         find("model"),
            "firmware":      find("firmwareVersion"),
            "serial":        find("serialNumber"),
            "device_type":   find("deviceType"),
        }

    async def channel_count(self) -> int:
        """Best-effort channel count for an NVR. Falls back to 1 (camera mode)."""
        try:
            r = await self._get("/ISAPI/ContentMgmt/InputProxy/channels")
            if r.status_code == 200:
                # Channels are listed inside <InputProxyChannelList>.
                count = r.text.count("<InputProxyChannel")
                if count > 0:
                    return count
        except Exception as e:
            log.debug("InputProxy/channels failed: %s", e)
        # Fallback: try /ISAPI/Streaming/channels — list of stream channels.
        try:
            r = await self._get("/ISAPI/Streaming/channels")
            if r.status_code == 200:
                # Count stream channels divisible by 2 (main+sub) or take id list.
                return max(r.text.count("<StreamingChannel "), 1)
        except Exception:
            pass
        return 1

    async def snapshot(self, channel: int = 1) -> bytes | None:
        """Returns raw JPEG bytes or None."""
        # Streaming channel id N0(stream): main = N01, sub = N02.
        path_id = channel * 100 + 1
        try:
            r = await self._get(f"/ISAPI/Streaming/channels/{path_id}/picture", timeout=10)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception as e:
            log.info("Hik snapshot ch=%s failed: %s", channel, e)
        return None
