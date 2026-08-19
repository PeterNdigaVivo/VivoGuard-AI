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
        # Port 443 is HTTPS by definition — building http://host:443
        # would just handshake garbage against the TLS socket.
        scheme = "https" if (ssl or int(http_port) == 443) else "http"
        self.base = f"{scheme}://{host}:{http_port}"
        self.username = username
        self.password = password

    async def _fetch(self, url: str, *, timeout: float) -> httpx.Response:
        # follow_redirects: some Dahua firmwares 302 every HTTP request
        # to HTTPS; verify=False because NVR certs are self-signed.
        async with httpx.AsyncClient(timeout=timeout, verify=False,
                                     follow_redirects=True) as c:
            # Try Digest first, fall back to Basic (older firmwares).
            r = await c.get(url, auth=httpx.DigestAuth(self.username, self.password))
            if r.status_code != 401:
                return r
            return await c.get(url, auth=(self.username, self.password))

    async def _get(self, path: str, *, timeout: float = 8.0) -> httpx.Response:
        r = await self._fetch(self.base + path, timeout=timeout)
        # HTTPS-redirect firmwares send `Location: https://host:443/` —
        # the CGI path is DROPPED, so even with follow_redirects we land
        # on the login page instead of the CGI output. Detect the hop to
        # https, re-issue the ORIGINAL path against the https origin,
        # and pin self.base there so later calls skip the dance.
        final = r.url
        if (r.history and final.scheme == "https"
                and not self.base.startswith("https://")):
            https_base = f"https://{final.host}:{final.port or 443}"
            wanted_path = path.split("?", 1)[0]
            if final.path != wanted_path or r.status_code >= 400:
                r2 = await self._fetch(https_base + path, timeout=timeout)
                if r2.status_code < 400:
                    log.info("Dahua %s redirected to HTTPS — retried on %s",
                             self.base, https_base)
                    self.base = https_base
                    return r2
            else:
                self.base = https_base   # redirect kept the path; pin https
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
