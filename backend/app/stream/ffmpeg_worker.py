"""One FFmpeg subprocess per camera.

We invoke ffmpeg with `-f mjpeg` output to stdout — each frame is a
self-delimited JPEG that we can split on SOI/EOI markers. This avoids the
need for piped raw YUV decoding and keeps memory predictable.

Frames land in Redis via `FrameBuffer.push_frame`. The worker also
writes a *health* row to Redis at three points so the Live View UI can
explain itself even when ffmpeg is blocked in connection negotiation:

  - "starting" — written the moment we call Popen, before any frame.
  - "running"  — every 5s while frames flow, with observed fps.
  - "hung"     — every 5s while ffmpeg is alive but no frame has
                 arrived yet (e.g. RTSP server is unreachable and the
                 socket is still waiting on -timeout). Without this,
                 the UI tile sat on 'Streamer not yet attempting' for
                 tens of seconds.
  - "exited"   — when ffmpeg dies, with stderr captured.
"""
from __future__ import annotations
import logging
import shlex
import subprocess
import threading
import time

from app.stream.frame_buffer import FrameBuffer
from app.stream.reconnect import Backoff

log = logging.getLogger(__name__)


# JPEG markers — JPEGs always start with FFD8 and end with FFD9.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def _build_cmd(rtsp_url: str, *, fps: int, width: int = 640) -> str:
    """Build the ffmpeg command. Forces TCP transport, scales to 640w to
    keep AI bandwidth light, and emits MJPEG to stdout."""
    return (
        "ffmpeg -hide_banner -loglevel warning "
        "-rtsp_transport tcp "
        "-timeout 5000000 -fflags nobuffer -flags low_delay "
        f"-i {shlex.quote(rtsp_url)} "
        f"-vf scale={width}:-2,fps={fps} "
        "-f mjpeg -q:v 6 pipe:1"
    )


class FFmpegWorker(threading.Thread):
    """Owns one ffmpeg subprocess for a single camera, restarts on failure."""

    def __init__(self, camera_id: int, rtsp_url: str, *,
                 fps: int = 5, width: int = 640,
                 buffer: FrameBuffer | None = None):
        super().__init__(daemon=True, name=f"ffmpeg-{camera_id}")
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.fps = fps
        self.width = width
        self.buffer = buffer or FrameBuffer()
        self.backoff = Backoff()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _iter_jpegs(stream):
        buf = b""
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(SOI)
                if start == -1:
                    if len(buf) > 1_048_576:
                        buf = b""
                    break
                end = buf.find(EOI, start + 2)
                if end == -1:
                    buf = buf[start:]
                    break
                yield buf[start:end + 2]
                buf = buf[end + 2:]

    def _hb_thread(self, proc: subprocess.Popen, started_at: float) -> None:
        """Heartbeat thread. While ffmpeg is alive AND no real frame has
        landed since `started_at`, push a 'Connecting to RTSP…' status
        every 5s. Stops when ffmpeg dies OR a frame arrives."""
        while not self._stop.is_set() and proc.poll() is None:
            time.sleep(5)
            if self._stop.is_set() or proc.poll() is not None:
                break
            health = self.buffer.health(self.camera_id) or {}
            # `last_frame_at` is bumped only by push_frame() — so any
            # value > started_at means we got a real JPEG since this run
            # began. In that case we stop writing the 'connecting' message.
            last_frame_at = float(health.get("last_frame_at") or 0)
            if last_frame_at >= started_at:
                return
            elapsed = int(time.time() - started_at)
            self.buffer.update_health(
                self.camera_id, fps=0,
                error=f"Connecting to RTSP… ({elapsed}s, ffmpeg still negotiating)",
            )

    def run(self) -> None:
        last_emit = time.time()
        frames_in_window = 0
        while not self._stop.is_set():
            # Mark the camera as "attempting" the moment we enter the
            # loop. Without this, a hang in Popen / RTSP negotiation
            # left no Redis health key, and the UI tile said
            # 'Streamer not yet attempting' for tens of seconds.
            self.buffer.update_health(self.camera_id, fps=0,
                                      error="Starting ffmpeg…")
            cmd = _build_cmd(self.rtsp_url, fps=self.fps, width=self.width)
            log.info("camera %s: starting ffmpeg", self.camera_id)
            started_at = time.time()
            try:
                proc = subprocess.Popen(
                    cmd, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except Exception as e:
                log.exception("camera %s: ffmpeg spawn failed: %s", self.camera_id, e)
                self.buffer.update_health(self.camera_id, fps=0, error=str(e))
                if self._stop.wait(self.backoff.fail()):
                    return
                continue

            # Background heartbeat keeps the UI informed during long
            # RTSP handshakes.
            hb = threading.Thread(target=self._hb_thread,
                                  args=(proc, started_at),
                                  daemon=True, name=f"hb-{self.camera_id}")
            hb.start()

            try:
                for jpeg in self._iter_jpegs(proc.stdout):
                    if self._stop.is_set():
                        break
                    self.buffer.push_frame(self.camera_id, jpeg)
                    frames_in_window += 1
                    now = time.time()
                    if now - last_emit >= 5:
                        observed = frames_in_window / (now - last_emit)
                        self.buffer.update_health(self.camera_id, fps=observed)
                        frames_in_window = 0
                        last_emit = now
                    self.backoff.succeed()
            finally:
                err = ""
                try:
                    err = (proc.stderr.read() or b"").decode(errors="ignore")
                except Exception:
                    pass
                proc.kill()
                proc.wait(timeout=3)

            if self._stop.is_set():
                return
            wait = self.backoff.fail()
            err_msg = err.strip()[:200] or "stream ended"
            log.warning("camera %s: ffmpeg exited (err=%s) — retry in %ds",
                        self.camera_id, err_msg[:120], wait)
            self.buffer.update_health(self.camera_id, fps=0,
                                       error=err_msg)
            # During the backoff wait, refresh the health row every 5s
            # with a 'Retrying in Xs (last error: …)' message. Without
            # this, long waits (40s, 80s, 5min) make the Live View tile
            # appear to forget what was happening.
            deadline = time.time() + wait
            while not self._stop.is_set() and time.time() < deadline:
                remaining = max(0, int(deadline - time.time()))
                self.buffer.update_health(
                    self.camera_id, fps=0,
                    error=f"Retrying in {remaining}s (last error: {err_msg[:150]})",
                )
                if self._stop.wait(min(5, remaining + 0.1)):
                    return
