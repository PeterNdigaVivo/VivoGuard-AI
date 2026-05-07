"""Dahua integration via the documented HTTP CGI surface.

Useful endpoints:
  /cgi-bin/magicBox.cgi?action=getDeviceType
  /cgi-bin/magicBox.cgi?action=getSerialNo
  /cgi-bin/magicBox.cgi?action=getSystemInfo
  /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle
  /cgi-bin/snapshot.cgi?channel=N         — JPEG snapshot

Dahua uses HTTP Digest auth on most firmwares (Basic on older ones; we try
Digest first and fall back).
"""
from __future__ import annotations
import logging
import httpx

log = logging.getLogger(__name__)


class DahuaHTTP:
    def __init__(self, host: str, http_port: int, username: str, password: str, *, ssl: bool = False):
        scheme = "https" if ssl else "http"
        self.base = f"{scheme}://{host}:{http_port}"
        self.username = username
        self.password = password

    async def _get(self, path: str, *, timeout: float = 8.0) -> httpx.Response:
        url = self.base + path
        # Try Digest first.
        async with httpx.AsyncClient(timeout=timeout, verify=False) as c:
            r = await c.get(url, auth=httpx.DigestAuth(self.username, self.password))
            if r.status_code != 401:
                return r
            # Fall back to Basic.
            r = await c.get(url, auth=(self.username, self.password))
            return r

    async def device_info(self) -> dict:
        r = await self._get("/cgi-bin/magicBox.cgi?action=getSystemInfo")
        r.raise_for_status()
        # Response is `key=value` lines.
        info: dict = {}
        for line in r.text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                info[k.strip()] = v.strip()

        # Add device type if missing.
        try:
            r2 = await self._get("/cgi-bin/magicBox.cgi?action=getDeviceType")
            if r2.status_code == 200 and "=" in r2.text:
                info["deviceType"] = r2.text.split("=", 1)[1].strip()
        except Exception:
            pass
        return info

    async def channel_count(self) -> int:
        """Use ChannelTitle config — number of entries == channel count."""
        try:
            r = await self._get("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
            if r.status_code == 200:
                # Lines look like `table.ChannelTitle[N].Name=Cam-N`.
                indices = set()
                for line in r.text.splitlines():
                    if "ChannelTitle[" in line:
                        try:
                            i = int(line.split("[")[1].split("]")[0])
                            indices.add(i)
                        except Exception:
                            continue
                if indices:
                    return max(indices) + 1   # Dahua is 0-indexed in config
        except Exception as e:
            log.debug("ChannelTitle fetch failed: %s", e)
        # Fallback: try recordManager caps.
        try:
            r = await self._get("/cgi-bin/recordManager.cgi?action=getCaps")
            for line in r.text.splitlines():
                if line.startswith("caps.MaxRemoteChannels="):
                    return int(line.split("=", 1)[1])
        except Exception:
            pass
        return 1

    async def snapshot(self, channel: int = 1) -> bytes | None:
        try:
            r = await self._get(f"/cgi-bin/snapshot.cgi?channel={channel}", timeout=10)
            if r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff":
                return r.content
        except Exception as e:
            log.info("Dahua snapshot ch=%s failed: %s", channel, e)
        return None
