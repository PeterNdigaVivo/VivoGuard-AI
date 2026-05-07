"""WebSocket endpoints — alert feed, live frames, training-log tail.

These use Redis pub/sub as the fan-out so multiple browser clients can
listen without coupling to the inference worker.
"""
from __future__ import annotations
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    """Push every alert as JSON to connected clients."""
    await ws.accept()
    r = aioredis.from_url(settings.redis_url)
    psub = r.pubsub()
    await psub.subscribe("vg:pub:alerts")
    try:
        async for msg in psub.listen():
            if msg.get("type") != "message":
                continue
            data = msg["data"]
            if isinstance(data, bytes):
                data = data.decode()
            try:
                # Validate it's JSON before forwarding.
                json.loads(data)
                await ws.send_text(data)
            except Exception:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        await psub.unsubscribe("vg:pub:alerts")
        await psub.close()


@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(ws: WebSocket, camera_id: int) -> None:
    """Forward JPEG frames from `vg:pub:frames:{id}` as binary messages."""
    await ws.accept()
    r = aioredis.from_url(settings.redis_url)
    psub = r.pubsub()
    chan = f"vg:pub:frames:{camera_id}"
    await psub.subscribe(chan)
    try:
        async for msg in psub.listen():
            if msg.get("type") != "message":
                continue
            data = msg["data"]
            if isinstance(data, bytes):
                await ws.send_bytes(data)
    except WebSocketDisconnect:
        pass
    finally:
        await psub.unsubscribe(chan)
        await psub.close()


@router.websocket("/ws/training/{job_id}")
async def ws_training(ws: WebSocket, job_id: int) -> None:
    """Tail the live training log written by the trainer.

    The trainer publishes lines to channel `vg:pub:training:{job_id}`.
    """
    await ws.accept()
    r = aioredis.from_url(settings.redis_url)
    psub = r.pubsub()
    chan = f"vg:pub:training:{job_id}"
    await psub.subscribe(chan)
    try:
        async for msg in psub.listen():
            if msg.get("type") != "message":
                continue
            data = msg["data"]
            await ws.send_text(data.decode() if isinstance(data, bytes) else str(data))
    except WebSocketDisconnect:
        pass
    finally:
        await psub.unsubscribe(chan)
        await psub.close()


# The alert-engine notifier loop is started from `main.lifespan`.
