"""Redis-backed frame buffer.

Per-camera storage layout:
  vg:frame:{camera_id}        — latest JPEG (TTL 30s); used by snapshot endpoints
  vg:health:{camera_id}       — JSON with fps/last_frame_at/error
  channel: vg:pub:frames:{id} — pub/sub stream of JPEG bytes for live MJPEG

The streamer service writes; the API + inference worker read.
"""
from __future__ import annotations
import json
import time
from typing import Optional

import redis

from app.config import settings


def _redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=False)


class FrameBuffer:
    def __init__(self, r: redis.Redis | None = None):
        self.r = r or _redis()

    # ----- write path (streamer) -----
    def push_frame(self, camera_id: int, jpeg: bytes, *, ttl: int = 30) -> None:
        key = f"vg:frame:{camera_id}".encode()
        self.r.set(key, jpeg, ex=ttl)
        # Pub/sub for live MJPEG consumers (don't block on no subscribers).
        try:
            self.r.publish(f"vg:pub:frames:{camera_id}", jpeg)
        except Exception:
            pass

    def update_health(self, camera_id: int, *, fps: float, error: Optional[str] = None) -> None:
        payload = {
            "camera_id": camera_id,
            "fps": round(fps, 2),
            "last_frame_at": time.time(),
            "error": error,
        }
        self.r.set(f"vg:health:{camera_id}", json.dumps(payload).encode(), ex=120)

    # ----- read path (api / inference) -----
    def latest_jpeg(self, camera_id: int) -> bytes | None:
        return self.r.get(f"vg:frame:{camera_id}".encode())

    def health(self, camera_id: int) -> dict | None:
        raw = self.r.get(f"vg:health:{camera_id}")
        return json.loads(raw) if raw else None

    def subscribe(self, camera_id: int):
        ps = self.r.pubsub()
        ps.subscribe(f"vg:pub:frames:{camera_id}")
        return ps
