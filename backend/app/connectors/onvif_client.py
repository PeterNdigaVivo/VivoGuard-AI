"""ONVIF integration: GetStreamUri, GetSnapshotUri, ContinuousMove (PTZ).

Uses onvif-zeep-async. Imported lazily so the backend image still boots
without the package — useful for stripped-down deployments.
"""
from __future__ import annotations
import logging
import httpx

log = logging.getLogger(__name__)


class ONVIFClient:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._cam = None

    async def _connect(self):
        from onvif import ONVIFCamera   # type: ignore
        if self._cam is None:
            self._cam = ONVIFCamera(self.host, self.port, self.username, self.password)
            await self._cam.update_xaddrs()
        return self._cam

    async def device_info(self) -> dict:
        cam = await self._connect()
        info = await cam.devicemgmt.GetDeviceInformation()
        return {
            "manufacturer": info.Manufacturer,
            "model":        info.Model,
            "firmware":     info.FirmwareVersion,
            "serial":       info.SerialNumber,
        }

    async def stream_uri(self, profile_index: int = 0) -> str | None:
        cam = await self._connect()
        media = await cam.create_media_service()
        profiles = await media.GetProfiles()
        if not profiles:
            return None
        token = profiles[profile_index].token
        req = media.create_type("GetStreamUri")
        req.ProfileToken = token
        req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        result = await media.GetStreamUri(req)
        return result.Uri

    async def snapshot(self, profile_index: int = 0) -> bytes | None:
        cam = await self._connect()
        media = await cam.create_media_service()
        profiles = await media.GetProfiles()
        if not profiles:
            return None
        token = profiles[profile_index].token
        req = media.create_type("GetSnapshotUri")
        req.ProfileToken = token
        result = await media.GetSnapshotUri(req)
        if not result or not result.Uri:
            return None
        # ONVIF snapshot URIs typically need the same credentials.
        async with httpx.AsyncClient(timeout=8.0, verify=False) as c:
            r = await c.get(result.Uri, auth=(self.username, self.password))
            if r.status_code == 200:
                return r.content
        return None

    async def ptz_move(self, pan: float, tilt: float, zoom: float = 0.0) -> None:
        """Continuous PTZ move; values in [-1, 1]. Caller is responsible for stop."""
        cam = await self._connect()
        media = await cam.create_media_service()
        ptz   = await cam.create_ptz_service()
        profiles = await media.GetProfiles()
        if not profiles:
            return
        token = profiles[0].token
        req = ptz.create_type("ContinuousMove")
        req.ProfileToken = token
        req.Velocity = {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}
        await ptz.ContinuousMove(req)

    async def ptz_stop(self) -> None:
        cam = await self._connect()
        media = await cam.create_media_service()
        ptz   = await cam.create_ptz_service()
        profiles = await media.GetProfiles()
        if not profiles:
            return
        req = ptz.create_type("Stop")
        req.ProfileToken = profiles[0].token
        req.PanTilt = True
        req.Zoom = True
        await ptz.Stop(req)
