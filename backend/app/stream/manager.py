"""Stream manager: keeps one worker alive per active camera.

Each camera has a `transport` field; we pick the right worker class:
  - 'rtsp'           → FFmpegWorker pulling H.264 over port 554
  - 'http_snapshot'  → HttpSnapshotWorker polling Dahua/generic CGI
                       over the HTTP port (works when 554 is blocked)

Owns the worker lifecycle. The streamer service drives this via a
poll loop that reconciles the desired set against the running set.
"""
from __future__ import annotations
import logging
import threading
from dataclasses import dataclass

from app.stream.ffmpeg_worker import FFmpegWorker
from app.stream.frame_buffer import FrameBuffer
from app.stream.http_snapshot_worker import HttpSnapshotWorker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraSpec:
    camera_id: int
    rtsp_url: str                       # main URL (RTSP) for FFmpeg path
    fps: int = 5
    width: int = 640
    # Transport mode + companion fields used only by the HTTP path.
    transport: str = "rtsp"
    snapshot_url: str = ""              # full HTTP URL with embedded {channel}
    username: str = ""
    password: str = ""


class StreamManager:
    def __init__(self, buffer: FrameBuffer | None = None):
        self.buffer  = buffer or FrameBuffer()
        # threading.Thread is the common base for both worker classes.
        self.workers: dict[int, threading.Thread] = {}
        self.specs:   dict[int, CameraSpec]       = {}

    @staticmethod
    def _redact(url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(url)
        netloc = (u.hostname or "") + (f":{u.port}" if u.port else "")
        if u.username:
            netloc = f"{u.username}:****@{netloc}"
        return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))

    def _spawn_worker(self, spec: CameraSpec) -> threading.Thread:
        if spec.transport == "http_snapshot":
            log.info("starting HTTP-snapshot worker camera=%s fps=%s url=%s",
                     spec.camera_id, spec.fps, self._redact(spec.snapshot_url))
            return HttpSnapshotWorker(
                spec.camera_id, spec.snapshot_url,
                fps=spec.fps,
                username=spec.username, password=spec.password,
                buffer=self.buffer,
            )
        log.info("starting RTSP worker camera=%s fps=%s url=%s",
                 spec.camera_id, spec.fps, self._redact(spec.rtsp_url))
        return FFmpegWorker(
            spec.camera_id, spec.rtsp_url,
            fps=spec.fps, width=spec.width, buffer=self.buffer,
        )

    def reconcile(self, desired: list[CameraSpec]) -> None:
        desired_by_id = {d.camera_id: d for d in desired}
        log.info("reconcile: %d desired cameras, %d running workers",
                 len(desired_by_id), len(self.workers))

        # Stop removed cameras.
        for cam_id in list(self.workers):
            if cam_id not in desired_by_id:
                log.info("stopping worker for camera %s", cam_id)
                self.workers[cam_id].stop()       # both worker classes expose stop()
                self.workers.pop(cam_id, None)
                self.specs.pop(cam_id, None)

        # Start or restart changed cameras.
        for cam_id, spec in desired_by_id.items():
            existing = self.specs.get(cam_id)
            if (existing == spec
                    and self.workers.get(cam_id)
                    and self.workers[cam_id].is_alive()):
                continue
            if cam_id in self.workers:
                self.workers[cam_id].stop()
            w = self._spawn_worker(spec)
            w.start()
            self.workers[cam_id] = w
            self.specs[cam_id]   = spec

    def stop_all(self) -> None:
        for w in self.workers.values():
            w.stop()
        self.workers.clear()
        self.specs.clear()
