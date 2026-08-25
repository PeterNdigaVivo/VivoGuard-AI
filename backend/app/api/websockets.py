"""WebSocket endpoints — alert feed, live frames, training-log tail.

These use Redis pub/sub as the fan-out so multiple browser clients can
listen without coupling to the inference worker.
"""
from __future__ import annotations
import json
import logging
from collections.abc import Collection

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import redis.asyncio as aioredis

from app.auth.security import decode_token
from app.config import settings
from app.database import SessionLocal
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter()

_AUTH_PROTOCOL = "vg-token"


def _offered_protocols(ws: WebSocket) -> list[str]:
    """Parse the browser's WebSocket subprotocol offer.

    Browsers cannot attach an Authorization header to a WebSocket handshake.
    The frontend therefore offers two subprotocols: the fixed ``vg-token``
    marker followed by the JWT. Unlike a query parameter this keeps the token
    out of URLs, reverse-proxy access logs and browser history.
    """
    return [part.strip() for part in
            ws.headers.get("sec-websocket-protocol", "").split(",")
            if part.strip()]


async def _authenticate(
    ws: WebSocket, *, roles: Collection[str] | None = None,
) -> User | None:
    """Authenticate and accept one WebSocket, or close it fail-closed."""
    protocols = _offered_protocols(ws)
    if len(protocols) < 2 or protocols[0] != _AUTH_PROTOCOL:
        await ws.close(code=4401, reason="authentication required")
        return None

    claims = decode_token(protocols[1])
    if not claims or claims.get("kind") != "access":
        await ws.close(code=4401, reason="invalid token")
        return None
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        await ws.close(code=4401, reason="invalid token")
        return None

    # A database lookup prevents a still-valid JWT from preserving access
    # after an administrator disables the account.
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            await ws.close(code=4401, reason="user disabled")
            return None
        if roles and user.role not in roles and user.role != "admin":
            await ws.close(code=4403, reason="insufficient role")
            return None
        # Detach the small identity object before the session closes.
        db.expunge(user)

    await ws.accept(subprotocol=_AUTH_PROTOCOL)
    return user


@router.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    """Push every alert as JSON to connected clients."""
    if await _authenticate(ws) is None:
        return
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
    if await _authenticate(ws) is None:
        return
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
    if await _authenticate(ws, roles={"admin", "operator"}) is None:
        return
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
