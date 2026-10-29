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
import os
import shlex
import signal
import subprocess
import threading
import time

from app.stream.frame_buffer import FrameBuffer
from app.stream.reconnect import Backoff
from app.utils.stream_secrets import redact_stream_credentials

log = logging.getLogger(__name__)


# JPEG markers — JPEGs always start with FFD8 and end with FFD9.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def _build_cmd(rtsp_url: str, *, fps: int, width: int = 640,
               rtsp_transport: str = "tcp", threads: int = 1) -> str:
    """Build the ffmpeg command. Scales to 640w to keep AI bandwidth
    light and emits MJPEG to stdout.

    `rtsp_transport`:
      'tcp'  (default) — reliable, works on most networks
      'http' — RTSP-over-HTTP tunnel. Use when the store router
               blocks 554 but forwards an HTTP port (80, 8000, 8080,
               7000, 800). The RTSP URL still says rtsp://, but
               FFmpeg negotiates over the HTTP port instead.
      'udp'  — lower latency on clean networks; not for the open
               internet.
    """
    return shlex.join(_build_argv(
        rtsp_url, fps=fps, width=width,
        rtsp_transport=rtsp_transport, threads=threads))


def _build_argv(rtsp_url: str, *, fps: int, width: int = 640,
                rtsp_transport: str = "tcp", threads: int = 1) -> list[str]:
    """The same command as an argv list, which is what we actually spawn.

    Popen(shell=True) put a /bin/sh between us and ffmpeg. Whether that
    shell `exec`s away or stays as a parent depends on the shell and on
    the exact command, so `proc.kill()` sometimes signalled the shell and
    left ffmpeg running — holding an RTSP connection and a CPU core with
    no owner. Spawning argv directly removes the question entirely.
    """
    if rtsp_transport not in ("tcp", "http", "udp"):
        rtsp_transport = "tcp"
    threads = max(1, int(threads))
    t = str(threads)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", rtsp_transport,
        "-timeout", "5000000", "-fflags", "nobuffer", "-flags", "low_delay",
        "-threads", t, "-filter_threads", t,
        "-i", rtsp_url,
        "-vf", f"scale={width}:-2,fps={fps}",
        "-threads", t,
        "-f", "mjpeg", "-q:v", "6", "pipe:1",
    ]


def _kill_tree(proc) -> None:
    """Kill the child AND anything it spawned, then fall back to the
    plain kill.

    The worker spawns with ``start_new_session=True``, so the child leads
    its own process group and one killpg reaches every descendant. The
    group call is best-effort: it is skipped for a pid we cannot resolve
    (already reaped, or a test double), and ``proc.kill()`` still runs so
    a single child is always signalled exactly once.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        # Already dead, never started, not a real pid, or a platform
        # without process groups — proc.kill() below is the fallback.
        pass
    try:
        proc.kill()
    except OSError:
        # The process may exit between poll() and kill().
        pass


def _retry_url_after_failure(
    *, active_url: str, preferred_url: str,
    fallback_url: str, frames_received: int,
) -> str:
    """Fail back to the saved mainstream after any preferred-stream exit.

    A preferred substream may yield a few frames and then repeatedly collapse.
    Treating that as healthy made its Redis frame expire between retries. Stable
    substreams never take this path; cameras that do exit prioritise coverage.
    ``frames_received`` stays in the signature for explicit call-site telemetry
    and backwards compatibility with focused tests.
    """
    if (
        active_url == preferred_url
        and fallback_url
        and fallback_url != preferred_url
    ):
        return fallback_url
    return active_url


def _stream_has_stalled(
    *, last_frame_at: float, started_at: float,
    now: float, stall_seconds: float,
) -> bool:
    """Return true only after this FFmpeg run produced, then stopped, frames."""
    return (
        last_frame_at >= started_at
        and now - last_frame_at >= max(5.0, float(stall_seconds))
    )


class FFmpegWorker(threading.Thread):
    """Owns one ffmpeg subprocess for a single camera, restarts on failure."""

    def __init__(self, camera_id: int, rtsp_url: str, *,
                 fallback_rtsp_url: str = "",
                 fps: int = 5, width: int = 640,
                 rtsp_transport: str = "tcp",
                 threads: int | None = None,
                 buffer: FrameBuffer | None = None):
        super().__init__(daemon=True, name=f"ffmpeg-{camera_id}")
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.fallback_rtsp_url = fallback_rtsp_url
        self.fps = fps
        self.width = width
        self.rtsp_transport = rtsp_transport
        self.threads = max(1, int(
            threads if threads is not None
            else os.environ.get("STREAMER_FFMPEG_THREADS", "1")
        ))
        self.stall_seconds = max(10, int(os.environ.get(
            "STREAMER_FRAME_STALL_SECONDS", "20",
        )))
        self.buffer = buffer or FrameBuffer()
        self.backoff = Backoff()
        self._stop = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self._stop.set()
        # A worker can be blocked indefinitely in ``stdout.read()`` while an
        # RTSP connection is alive but producing no JPEGs. Setting the event
        # alone cannot wake that read, so a reconcile that replaces the worker
        # would leave its FFmpeg child running beside the replacement.
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)

    def _own_proc(self, proc: subprocess.Popen) -> None:
        """Publish the active child and close the stop/spawn race."""
        with self._proc_lock:
            self._proc = proc
            stopped = self._stop.is_set()
        if stopped and proc.poll() is None:
            _kill_tree(proc)

    def _release_proc(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            if self._proc is proc:
                self._proc = None

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

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen, into: list[bytes]) -> None:
        """Read ffmpeg's stderr until EOF, keeping only the last 64 KiB.

        Runs for the life of the child so the pipe buffer can never fill
        and wedge ffmpeg, and so the teardown has the diagnostics already
        in hand rather than having to block for them.
        """
        try:
            while True:
                chunk = proc.stderr.read(8192)
                if not chunk:
                    return
                into.append(chunk)
                if len(into) > 8:
                    del into[:-8]
        except Exception:
            # Pipe closed under us during teardown — the caller only
            # wants whatever was collected before that.
            return

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
            # value > started_at means we got a real JPEG since this run.
            last_frame_at = float(health.get("last_frame_at") or 0)
            now = time.time()
            if _stream_has_stalled(
                last_frame_at=last_frame_at,
                started_at=started_at,
                now=now,
                stall_seconds=self.stall_seconds,
            ):
                self.buffer.update_health(
                    self.camera_id, fps=0,
                    error="Frame stream stalled; restarting ffmpeg…",
                )
                log.warning(
                    "camera %s: no frame for %.1fs; restarting ffmpeg",
                    self.camera_id, now - last_frame_at,
                )
                # A stalled ffmpeg is exactly the case that used to leak:
                # kill the whole group, not just the process we can see.
                _kill_tree(proc)
                return
            if last_frame_at >= started_at:
                continue
            elapsed = int(now - started_at)
            self.buffer.update_health(
                self.camera_id, fps=0,
                error=f"Connecting to RTSP… ({elapsed}s, ffmpeg still negotiating)",
            )

    def run(self) -> None:
        last_emit = time.time()
        frames_in_window = 0
        active_url = self.rtsp_url
        while not self._stop.is_set():
            # Mark the camera as "attempting" the moment we enter the
            # loop. Without this, a hang in Popen / RTSP negotiation
            # left no Redis health key, and the UI tile said
            # 'Streamer not yet attempting' for tens of seconds.
            self.buffer.update_health(self.camera_id, fps=0,
                                      error="Starting ffmpeg…")
            argv = _build_argv(
                active_url,
                fps=self.fps,
                width=self.width,
                rtsp_transport=self.rtsp_transport,
                threads=self.threads,
            )
            log.info("camera %s: starting ffmpeg", self.camera_id)
            started_at = time.time()
            frames_this_run = 0
            try:
                # No shell, and its own process group: one killpg then
                # reaches ffmpeg and anything it spawned, whatever the
                # platform's shell would have done with the command.
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0, start_new_session=True,
                )
                self._own_proc(proc)
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

            # Drain stderr continuously instead of reading it at the end.
            # `proc.stderr.read()` returns only at EOF, i.e. when ffmpeg
            # exits — so reading it BEFORE killing (which is what the
            # teardown below used to do) blocked forever whenever ffmpeg
            # closed stdout but stayed alive. The worker thread parked
            # there holding a live child, the manager still saw the thread
            # as alive so never replaced it, and the children stacked up:
            # 260 of them on one host, 442% of CPU, until the dashboard
            # could not answer a request inside 8 seconds.
            err_chunks: list[bytes] = []
            drain = threading.Thread(
                target=self._drain_stderr, args=(proc, err_chunks),
                daemon=True, name=f"err-{self.camera_id}")
            drain.start()

            try:
                for jpeg in self._iter_jpegs(proc.stdout):
                    if self._stop.is_set():
                        break
                    self.buffer.push_frame(self.camera_id, jpeg)
                    frames_in_window += 1
                    frames_this_run += 1
                    now = time.time()
                    if now - last_emit >= 5:
                        observed = frames_in_window / (now - last_emit)
                        self.buffer.update_health(self.camera_id, fps=observed)
                        frames_in_window = 0
                        last_emit = now
                    self.backoff.succeed()
            finally:
                # Kill FIRST, collect diagnostics after. The order is the
                # fix: nothing in this block may wait on a process we have
                # not yet signalled.
                try:
                    if proc.poll() is None:
                        _kill_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        log.error("camera %s: ffmpeg pid %s ignored SIGKILL",
                                  self.camera_id, proc.pid)
                finally:
                    self._release_proc(proc)
                # The drain thread ends at EOF, which the kill guarantees.
                drain.join(timeout=5)
                err = b"".join(err_chunks).decode(errors="ignore")

            if self._stop.is_set():
                return
            retry_url = _retry_url_after_failure(
                active_url=active_url,
                preferred_url=self.rtsp_url,
                fallback_url=self.fallback_rtsp_url,
                frames_received=frames_this_run,
            )
            if retry_url != active_url:
                active_url = retry_url
                log.warning(
                    "camera %s: preferred substream exited after %d frames; "
                    "falling back to saved mainstream",
                    self.camera_id, frames_this_run,
                )
            wait = self.backoff.fail()
            # FFmpeg repeats its input URL in many failures. Camera URLs carry
            # NVR credentials, so the raw stderr must never reach logs, Redis
            # health state, or the Live View UI.
            err_msg = redact_stream_credentials(err).strip()[:200] or "stream ended"
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
