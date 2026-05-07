"""Stream manager: keeps one FFmpegWorker alive per active camera.

Owns the worker lifecycle. The streamer service drives this with a poll
loop that reconciles the desired set of active cameras against the
running set.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

from app.stream.ffmpeg_worker import FFmpegWorker
from app.stream.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraSpec:
    camera_id: int
    rtsp_url: str
    fps: int = 5
    width: int = 640


class StreamManager:
    def __init__(self, buffer: FrameBuffer | None = None):
        self.buffer  = buffer or FrameBuffer()
        self.workers: dict[int, FFmpegWorker] = {}
        self.specs:   dict[int, CameraSpec]   = {}

    def reconcile(self, desired: list[CameraSpec]) -> None:
        """Bring the live set of workers in line with `desired`.
        Workers are started/stopped/restarted as needed; URL changes
        trigger a restart (simplest correct behaviour)."""
        desired_by_id = {d.camera_id: d for d in desired}

        # Stop removed cameras.
        for cam_id in list(self.workers):
            if cam_id not in desired_by_id:
                log.info("stopping worker for camera %s", cam_id)
                self.workers[cam_id].stop()
                self.workers.pop(cam_id, None)
                self.specs.pop(cam_id, None)

        # Start or restart changed cameras.
        for cam_id, spec in desired_by_id.items():
            existing = self.specs.get(cam_id)
            if existing == spec and self.workers.get(cam_id) and self.workers[cam_id].is_alive():
                continue
            if cam_id in self.workers:
                self.workers[cam_id].stop()
            log.info("starting worker for camera %s -> %s", cam_id, spec.rtsp_url[:64])
            w = FFmpegWorker(spec.camera_id, spec.rtsp_url,
                             fps=spec.fps, width=spec.width, buffer=self.buffer)
            w.start()
            self.workers[cam_id] = w
            self.specs[cam_id]   = spec

    def stop_all(self) -> None:
        for w in self.workers.values():
            w.stop()
        self.workers.clear()
        self.specs.clear()
