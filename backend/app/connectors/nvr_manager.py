"""NVR-side coordination: log in, enumerate channels, persist as cameras.

This is the orchestrator the API layer calls when the user clicks
"Connect NVR". Vendor specifics live in dahua_http / hikvision_isapi.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

from app.connectors.dahua_http import DahuaHTTP
from app.connectors.hikvision_isapi import HikvisionISAPI
from app.utils.network import build_rtsp_url

log = logging.getLogger(__name__)


@dataclass
class DiscoveredChannel:
    channel: int
    name: str
    rtsp_main: str
    rtsp_sub: str


async def enumerate_dahua(host: str, http_port: int, rtsp_port: int,
                          username: str, password: str) -> list[DiscoveredChannel]:
    api = DahuaHTTP(host, http_port, username, password)
    count = await api.channel_count()
    log.info("Dahua %s reports %d channel(s)", host, count)
    out = []
    for ch in range(1, count + 1):
        out.append(DiscoveredChannel(
            channel=ch,
            name=f"Channel {ch}",
            rtsp_main=build_rtsp_url(brand="dahua", host=host, port=rtsp_port,
                                     username=username, password=password,
                                     channel=ch, subtype=0),
            rtsp_sub=build_rtsp_url(brand="dahua", host=host, port=rtsp_port,
                                    username=username, password=password,
                                    channel=ch, subtype=1),
        ))
    return out


async def enumerate_hikvision(host: str, http_port: int, rtsp_port: int,
                              username: str, password: str) -> list[DiscoveredChannel]:
    api = HikvisionISAPI(host, http_port, username, password)
    count = await api.channel_count()
    log.info("Hikvision %s reports %d channel(s)", host, count)
    out = []
    for ch in range(1, count + 1):
        out.append(DiscoveredChannel(
            channel=ch,
            name=f"Channel {ch}",
            rtsp_main=build_rtsp_url(brand="hikvision", host=host, port=rtsp_port,
                                     username=username, password=password,
                                     channel=ch, subtype=0),
            rtsp_sub=build_rtsp_url(brand="hikvision", host=host, port=rtsp_port,
                                    username=username, password=password,
                                    channel=ch, subtype=1),
        ))
    return out
