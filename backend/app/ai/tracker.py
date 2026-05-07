"""Lightweight IOU tracker.

Sufficient for stateful detection types (loitering, abandoned object,
tailgating, occupancy continuity). For high-volume tracking use
ByteTrack / Norfair; this is a deliberately minimal implementation kept
in-process so we don't need a separate tracking service.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from app.ai.zone_logic import iou


@dataclass
class Track:
    track_id: int
    cls: str
    bbox_norm: list[float]
    first_seen: float
    last_seen: float
    history: list[list[float]] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class IOUTracker:
    def __init__(self, iou_threshold: float = 0.3, max_age_seconds: float = 3.0):
        self.iou_threshold = iou_threshold
        self.max_age_seconds = max_age_seconds
        self.tracks: dict[int, Track] = {}
        self._next_id = 1

    def update(self, detections: list[dict]) -> list[tuple[Track, dict]]:
        """Match new detections to existing tracks; return (track, det) pairs.
        Unmatched detections become new tracks; stale tracks are aged out."""
        now = time.time()
        matched: dict[int, dict] = {}
        unmatched_dets: list[dict] = []

        for det in detections:
            best_id, best_iou = -1, 0.0
            for tid, tr in self.tracks.items():
                if tr.cls != det["cls"]:
                    continue
                if tid in matched:
                    continue
                u = iou(tr.bbox_norm, det["bbox_norm"])
                if u > best_iou:
                    best_id, best_iou = tid, u
            if best_iou >= self.iou_threshold:
                matched[best_id] = det
            else:
                unmatched_dets.append(det)

        # Update matched.
        out: list[tuple[Track, dict]] = []
        for tid, det in matched.items():
            tr = self.tracks[tid]
            tr.bbox_norm = det["bbox_norm"]
            tr.last_seen = now
            tr.history.append(det["bbox_norm"])
            if len(tr.history) > 64:
                tr.history = tr.history[-64:]
            out.append((tr, det))

        # Spawn new tracks.
        for det in unmatched_dets:
            tr = Track(
                track_id=self._next_id, cls=det["cls"], bbox_norm=det["bbox_norm"],
                first_seen=now, last_seen=now, history=[det["bbox_norm"]],
            )
            self._next_id += 1
            self.tracks[tr.track_id] = tr
            out.append((tr, det))

        # Age out stale.
        stale = [tid for tid, tr in self.tracks.items() if now - tr.last_seen > self.max_age_seconds]
        for tid in stale:
            self.tracks.pop(tid, None)

        return out
