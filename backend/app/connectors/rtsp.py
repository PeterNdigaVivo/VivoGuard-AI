"""RTSP probing — verify a stream URL works and grab a thumbnail.

Uses ffprobe (for liveness) and ffmpeg (for a JPEG frame). Both are present
in the backend image. We don't rely on python opencv for connection
testing because OpenCV's RTSP backend is finicky over WAN.
"""
from __future__ import annotations
import asyncio
import base64
import logging
import shlex
from pathlib import Path
import tempfile

log = logging.getLogger(__name__)


async def _run(cmd: str, timeout: float = 15.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode(errors="ignore"), err.decode(errors="ignore")
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"


async def probe_rtsp(url: str, timeout: float = 12.0,
                     rtsp_transport: str = "tcp") -> tuple[bool, str | None]:
    """Return (ok, error). `ok=True` means ffprobe could open the stream.

    `rtsp_transport` is the FFmpeg flag: 'tcp' (default), 'http' (RTSP
    over HTTP — for NVRs behind routers that block 554 but forward an
    HTTP port), or 'udp'.

    Uses `-timeout` (microseconds) — the canonical RTSP demuxer socket
    timeout in modern FFmpeg.
    """
    if rtsp_transport not in ("tcp", "http", "udp"):
        rtsp_transport = "tcp"
    cmd = (
        f"ffprobe -hide_banner -loglevel error -rtsp_transport {rtsp_transport} "
        f"-timeout 5000000 -i {shlex.quote(url)}"
    )
    code, _, err = await _run(cmd, timeout=timeout)
    if code == 0:
        return True, None
    log.info("ffprobe failed for %s (transport=%s): %s",
             url, rtsp_transport, err.strip()[:200])
    return False, err.strip()[:200] or f"ffprobe exit={code}"


async def grab_thumbnail(url: str, timeout: float = 15.0) -> str | None:
    """Capture a single JPEG frame and return it base64-encoded.
    Returns None on failure. Stderr is logged for debugging."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "thumb.jpg"
        cmd = (
            "ffmpeg -hide_banner -loglevel error -rtsp_transport tcp "
            f"-timeout 5000000 -y -i {shlex.quote(url)} -frames:v 1 -q:v 5 "
            f"{shlex.quote(str(out))}"
        )
        code, _, err = await _run(cmd, timeout=timeout)
        if code != 0 or not out.exists():
            log.info("grab_thumbnail failed (code=%s): %s", code, err.strip()[:300])
            return None
        return base64.b64encode(out.read_bytes()).decode()


async def grab_thumbnail_verbose(url: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """Like grab_thumbnail, but also returns FFmpeg stderr for the
    /diagnose endpoint so operators can see why a stream is failing."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "thumb.jpg"
        cmd = (
            "ffmpeg -hide_banner -loglevel info -rtsp_transport tcp "
            f"-timeout 5000000 -y -i {shlex.quote(url)} -frames:v 1 -q:v 5 "
            f"{shlex.quote(str(out))}"
        )
        code, _, err = await _run(cmd, timeout=timeout)
        b64 = None
        if code == 0 and out.exists():
            b64 = base64.b64encode(out.read_bytes()).decode()
        return b64, err.strip()[:4000]
