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
    def latest_jpeg(self, camera_id: int, *,
                    prefer_overlay: bool = True) -> bytes | None:
        """Return the latest JPEG. When `prefer_overlay` is True (the
        default for the snapshot / Live View paths) we serve the
        annotated `vg:frame_overlay:{id}` frame when one is fresh —
        that's where the QueueDetector writes the numbered-box + flow-
        line overlay — falling back to the raw `vg:frame:{id}` the
        streamer publishes. Inference reads should pass
        prefer_overlay=False so they detect on pristine pixels."""
        if prefer_overlay:
            overlay = self.r.get(f"vg:frame_overlay:{camera_id}".encode())
            if overlay:
                return overlay
        return self.r.get(f"vg:frame:{camera_id}".encode())

    def health(self, camera_id: int) -> dict | None:
        raw = self.r.get(f"vg:health:{camera_id}")
        return json.loads(raw) if raw else None

    def health_many(self, camera_ids: list[int]) -> dict[int, dict]:
        """Batched health() — one MGET instead of N round-trips. Cameras
        with no health row (or an unparseable one) are simply absent from
        the returned map."""
        if not camera_ids:
            return {}
        raws = self.r.mget([f"vg:health:{cid}" for cid in camera_ids])
        out: dict[int, dict] = {}
        for cid, raw in zip(camera_ids, raws):
            if not raw:
                continue
            try:
                out[cid] = json.loads(raw)
            except (ValueError, TypeError):
                continue
        return out

    def subscribe(self, camera_id: int):
        ps = self.r.pubsub()
        ps.subscribe(f"vg:pub:frames:{camera_id}")
        return ps
