import asyncio

import httpx

from app.connectors.hikvision_isapi import HikvisionISAPI


def test_channel_count_does_not_count_list_wrapper(monkeypatch) -> None:
    xml = """<?xml version="1.0"?>
    <InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <InputProxyChannel><id>1</id></InputProxyChannel>
      <InputProxyChannel><id>2</id></InputProxyChannel>
    </InputProxyChannelList>"""
    response = httpx.Response(200, text=xml)

    async def fake_get(_path: str, *, timeout: float = 8.0):
        return response

    client = HikvisionISAPI("192.0.2.1", 80, "admin", "secret")
    monkeypatch.setattr(client, "_get", fake_get)

    assert asyncio.run(client.channel_count()) == 2
