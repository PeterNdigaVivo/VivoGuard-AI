"""Polls an NVR's snapshot endpoint at N FPS, pushes each JPEG to Redis.

Use case: stores where RTSP (port 554) isn't forwarded by the router
but the NVR's HTTP port (e.g. Dahua default 80, or 7000 in some
installs) IS. Dahua exposes
    GET /cgi-bin/snapshot.cgi?channel=N
on the HTTP port, returning a fresh JPEG each request.

Drop-in interface match with FFmpegWorker: same threading model, same
FrameBuffer writes, same health heartbeat semantics. The StreamManager
picks one or the other based on `Camera.transport`.

Tradeoffs vs RTSP:
  + No port-554 forwarding needed.
  + Works with HTTP Digest / Basic auth (vendor-mixed).
  - ~5-10× the bandwidth of equivalent H.264 RTSP (each JPEG is
    a keyframe).
  - Max practical rate ~5 fps; the streamer caps at 5 for HTTP mode.
  - No audio.
"""
from __future__ import annotations
import logging
import threading
import time

import httpx

from app.stream.frame_buffer import FrameBuffer
from app.stream.reconnect import Backoff

log = logging.getLogger(__name__)


class HttpSnapshotWorker(threading.Thread):
    def __init__(self, camera_id: int, snapshot_url: str, *,
                 fps: int = 2,
                 username: str | None = None,
                 password: str | None = None,
                 buffer: FrameBuffer | None = None):
        super().__init__(daemon=True, name=f"http-{camera_id}")
        self.camera_id = camera_id
        self.snapshot_url = snapshot_url
        self.username = username or ""
        self.password = password or ""
        # Cap at 5 — HTTP polling at higher rates burns NVR CPU + bandwidth
        # without proportional accuracy gain for retail AI workloads.
        self.fps = max(1, min(int(fps), 5))
        self.buffer = buffer or FrameBuffer()
        self.backoff = Backoff()
        self._stop = threading.Event()
        # Persistent client so we reuse the TCP+TLS connection between
        # polls. The previous version constructed a fresh client every
        # GET, which at 5fps × 40 cameras is 200 socket setups/sec.
        # We use httpx HTTP/1.1 keep-alive by default.
        self._client: httpx.Client | None = None
        # Auth instance is built once. Falls back to Basic on first
        # 401 and remembers the choice.
        self._auth: httpx.Auth | tuple[str, str] | None = None
        if self.username:
            self._auth = httpx.DigestAuth(self.username, self.password)

    def stop(self) -> None:
        self._stop.set()
        # Close the keep-alive client so we don't leak sockets when
        # the StreamManager rebuilds workers during reconciliation.
        c = self._client
        self._client = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _get_jpeg(self) -> bytes:
        """One HTTP GET. Reuses the persistent client (keep-alive)
        and the previously-negotiated auth. Falls back from Digest to
        Basic on 401 once and sticks with whichever worked."""
        if self._client is None:
            self._client = httpx.Client(timeout=10.0, verify=False)
        r = self._client.get(self.snapshot_url, auth=self._auth)
        if r.status_code == 401 and self.username and isinstance(self._auth, httpx.DigestAuth):
            # Some old firmware uses Basic; retry once and remember.
            self._auth = (self.username, self.password)
            r = self._client.get(self.snapshot_url, auth=self._auth)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        if not r.content or r.content[:3] != b"\xff\xd8\xff":
            raise RuntimeError("response not a JPEG (got HTML / wrong endpoint?)")
        return r.content

    def run(self) -> None:
        log.info("camera %s: starting HTTP-snapshot worker (%dfps, %s)",
                 self.camera_id, self.fps, self._redacted_url())
        self.buffer.update_health(self.camera_id, fps=0,
                                  error="Starting HTTP snapshot poller…")
        sleep_s = 1.0 / self.fps
        last_emit = time.time()
        frames_in_window = 0
        while not self._stop.is_set():
            try:
                jpeg = self._get_jpeg()
                self.buffer.push_frame(self.camera_id, jpeg)
                frames_in_window += 1
                now = time.time()
                if now - last_emit >= 5:
                    observed = frames_in_window / (now - last_emit)
                    self.buffer.update_health(self.camera_id, fps=observed)
                    frames_in_window = 0
                    last_emit = now
                self.backoff.succeed()
            except Exception as e:
                msg = str(e)[:200]
                log.warning("camera %s HTTP snapshot failed: %s", self.camera_id, msg)
                self.buffer.update_health(self.camera_id, fps=0,
                                          error=f"HTTP snapshot: {msg}")
                wait = self.backoff.fail()
                # Refresh the health row every 5s during backoff so the
                # UI tile counts down instead of going silent.
                deadline = time.time() + wait
                while not self._stop.is_set() and time.time() < deadline:
                    remaining = max(0, int(deadline - time.time()))
                    self.buffer.update_health(
                        self.camera_id, fps=0,
                        error=f"Retrying in {remaining}s (last: {msg[:140]})",
                    )
                    if self._stop.wait(min(5, remaining + 0.1)):
                        return
                continue
            if self._stop.wait(sleep_s):
                return

    def _redacted_url(self) -> str:
        # snapshot_url shouldn't contain creds (we use httpx auth) but
        # belt-and-suspenders.
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(self.snapshot_url)
        netloc = (u.hostname or "") + (f":{u.port}" if u.port else "")
        if u.username:
            netloc = f"{u.username}:****@{netloc}"
        return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
