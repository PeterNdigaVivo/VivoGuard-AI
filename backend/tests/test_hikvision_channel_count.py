import asyncio

import httpx

from app.connectors.hikvision_isapi import HikvisionISAPI


def _response(xml: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=xml,
        request=httpx.Request("GET", "http://192.0.2.1/ISAPI/test"),
    )


def test_device_info_reads_namespaced_leaf_elements(monkeypatch) -> None:
    xml = """<?xml version="1.0"?>
    <DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <deviceName>Store NVR</deviceName>
      <model>DS-7608</model>
      <deviceType>NVR</deviceType>
    </DeviceInfo>"""

    async def fake_get(_path: str, *, timeout: float = 8.0):
        return _response(xml)

    client = HikvisionISAPI("192.0.2.1", 80, "admin", "secret")
    monkeypatch.setattr(client, "_get", fake_get)

    info = asyncio.run(client.device_info())
    assert info["device_name"] == "Store NVR"
    assert info["model"] == "DS-7608"
    assert info["device_type"] == "NVR"


def test_channel_count_does_not_count_list_wrapper(monkeypatch) -> None:
    xml = """<?xml version="1.0"?>
    <InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <InputProxyChannel><id>1</id></InputProxyChannel>
      <InputProxyChannel><id>2</id></InputProxyChannel>
    </InputProxyChannelList>"""
    response = _response(xml)

    async def fake_get(_path: str, *, timeout: float = 8.0):
        return response

    client = HikvisionISAPI("192.0.2.1", 80, "admin", "secret")
    monkeypatch.setattr(client, "_get", fake_get)

    assert asyncio.run(client.channel_count()) == 2
