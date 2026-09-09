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
    def __init__(self, host: str, http_port: int, username: str, password: str,
                 *, ssl: bool | None = None):
        scheme = "https" if (http_port == 443 if ssl is None else ssl) else "http"
        self.base = f"{scheme}://{host}:{http_port}"
        self.auth = httpx.DigestAuth(username, password)

    async def _get(self, path: str, *, timeout: float = 8.0) -> httpx.Response:
        async with httpx.AsyncClient(
            auth=self.auth, timeout=timeout, verify=False, follow_redirects=True,
        ) as c:
            return await c.get(self.base + path)

    @staticmethod
    def _local(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    @classmethod
    def _channel_ids(cls, xml: str, item_name: str) -> list[int]:
        root = ET.fromstring(xml)
        ids: list[int] = []
        for item in root.iter():
            if cls._local(item) != item_name:
                continue
            value = next(
                (child.text for child in item.iter()
                 if cls._local(child) == "id" and child.text),
                None,
            )
            if value and value.strip().isdigit():
                ids.append(int(value.strip()))
        return ids

    async def channel_numbers(self) -> list[int]:
        """Return physical camera channels, not main/sub-stream entries."""
        try:
            r = await self._get("/ISAPI/ContentMgmt/InputProxy/channels")
            if r.status_code == 200:
                ids = self._channel_ids(r.text, "InputProxyChannel")
                if ids:
                    return sorted(set(ids))
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            log.debug("InputProxy channel discovery failed: %s", exc)

        try:
            r = await self._get("/ISAPI/Streaming/channels")
            if r.status_code == 200:
                stream_ids = self._channel_ids(r.text, "StreamingChannel")
                channels = {stream_id // 100 for stream_id in stream_ids if stream_id >= 100}
                if channels:
                    return sorted(channels)
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            log.debug("Streaming channel discovery failed: %s", exc)
        return [1]

    async def device_info(self) -> dict:
        r = await self._get("/ISAPI/System/deviceInfo")
        r.raise_for_status()
        ns = "{http://www.hikvision.com/ver20/XMLSchema}"
        root = ET.fromstring(r.text)
        def find(tag: str) -> str:
            # ElementTree leaf elements are falsey, so using ``a or b``
            # discards a valid namespaced match and returns an empty value.
            el = root.find(f"{ns}{tag}")
            if el is None:
                el = root.find(tag)
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
        return len(await self.channel_numbers())

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
