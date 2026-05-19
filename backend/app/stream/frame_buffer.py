"""Redis-backed frame buffer.

Per-camera storage layout:
  vg:frame:{camera_id}        — latest JPEG (TTL 30s); used by snapshot endpoints
  vg:health:{camera_id}       — JSON with fps + last_health_at + last_frame_at + error
  channel: vg:pub:frames:{id} — pub/sub stream of JPEG bytes for live MJPEG

The streamer service writes; the API + inference worker read.

Two semantically distinct timestamps in `health`:
  - `last_health_at`  : when the streamer last wrote ANY status (kept alive
                        across long retry backoffs).
  - `last_frame_at`   : when an actual decoded JPEG last hit Redis (the
                        signal for `is_streaming`). Status writes that
                        report errors do NOT update this.
"""
from __future__ import annotations
import json
import time
from typing import Optional

import redis

from app.config import settings


# 24h TTL on the health key so that "Connection refused" stays visible
# on the Live View tile across long exponential-backoff waits between
# retries. Was 120s, which expired between attempts once the backoff
# crossed 2 minutes — operator saw "Streamer not yet attempting" again.
HEALTH_TTL_SECONDS = 86400


def _redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=False)


class FrameBuffer:
    def __init__(self, r: redis.Redis | None = None):
        self.r = r or _redis()

    # ----- write path (streamer) -----
    def push_frame(self, camera_id: int, jpeg: bytes, *, ttl: int = 30) -> None:
        key = f"vg:frame:{camera_id}".encode()
        self.r.set(key, jpeg, ex=ttl)
        try:
            self.r.publish(f"vg:pub:frames:{camera_id}", jpeg)
        except Exception:
            pass
        # Bump last_frame_at on the health record so the dashboard
        # knows we have a real frame, not just a status write.
        self._merge_health(camera_id, {"last_frame_at": time.time()})

    def update_health(self, camera_id: int, *, fps: float,
                      error: Optional[str] = None) -> None:
        """Status write. Updates last_health_at but NOT last_frame_at —
        the latter is only bumped by push_frame() so `is_streaming`
        means 'a real JPEG arrived recently'."""
        self._merge_health(camera_id, {
            "fps": round(fps, 2),
            "last_health_at": time.time(),
            "error": error,
        })

    def _merge_health(self, camera_id: int, patch: dict) -> None:
        """Read-modify-write so consecutive callers don't clobber each
        other's fields (e.g., the heartbeat thread updating just the
        status while a frame arrives elsewhere)."""
        key = f"vg:health:{camera_id}"
        raw = self.r.get(key)
        current: dict = {"camera_id": camera_id}
        if raw:
            try:
                current = json.loads(raw)
            except Exception:
                current = {"camera_id": camera_id}
        current.update(patch)
        self.r.set(key, json.dumps(current).encode(), ex=HEALTH_TTL_SECONDS)

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
