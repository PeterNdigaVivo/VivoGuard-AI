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


def is_static_track(history_norm: list[list[float]],
                    frame_wh: tuple[int, int], *,
                    min_px: float = 5.0, window: int = 10) -> bool:
    """True when a track has NOT moved — the mannequin signature.

    Judged on the track's own bbox history (both trackers maintain
    Track.history): max displacement of the bbox CENTRE across the last
    `window` positions versus the window's first position, in pixels.
    Max-vs-first (not end-to-end) so a person pacing back to their
    starting spot still reads as moving.

    Tracks younger than `window` frames return False — a person who
    just appeared cannot be judged static yet; mannequins persist and
    accumulate the full window within seconds, so they are filtered
    almost immediately while new real people are never suppressed."""
    if len(history_norm) < window:
        return False
    w, h = frame_wh
    pts = [(((bb[0] + bb[2]) / 2.0) * w, ((bb[1] + bb[3]) / 2.0) * h)
           for bb in history_norm[-window:]]
    x0, y0 = pts[0]
    max_d = max(((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 for x, y in pts[1:])
    return max_d < float(min_px)


# ═══════════════════════════════════════════════════════════════════════════
# ByteTrack adapter (Roboflow Supervision) — drop-in for IOUTracker.
#
# VivoGuardTracker.update() returns the SAME list[(Track, det)] contract as
# IOUTracker, so the ~25 downstream detectors are untouched: ByteTrack runs
# behind the adapter and its tracker_id becomes Track.track_id. The ORIGINAL
# detection dict is reused (only track_id is attached) — never reconstructed —
# so bbox precision, cls strings, and any future keys pass through verbatim.
# ═══════════════════════════════════════════════════════════════════════════
from collections import deque

TRACK_HISTORY_LENGTH   = 64      # max bbox_norm history kept per Track
CONF_BUFFER_WINDOW     = 10      # rolling-confidence window (frames)
BYTETRACK_ACTIVATION   = 0.25
BYTETRACK_LOST_BUFFER  = 30      # frames a track survives after last seen
BYTETRACK_MATCH_THRESH = 0.8
INFERENCE_FPS          = 3


class TrackConfidenceBuffer:
    """Rolling per-tracker_id confidence average — smooths single-frame
    misclassification. Values are surfaced onto Track.extra by the adapter."""

    def __init__(self, window: int = CONF_BUFFER_WINDOW):
        self.window = window
        self.buffers: dict[int, deque] = {}

    def update(self, tracker_id: int, confidence: float) -> None:
        if tracker_id not in self.buffers:
            self.buffers[tracker_id] = deque(maxlen=self.window)
        self.buffers[tracker_id].append(float(confidence))

    def get_avg(self, tracker_id: int) -> float:
        buf = self.buffers.get(tracker_id)
        return sum(buf) / len(buf) if buf else 0.0

    def frames_seen(self, tracker_id: int) -> int:
        buf = self.buffers.get(tracker_id)
        return len(buf) if buf else 0

    def is_confident(self, tracker_id: int, threshold: float = 0.6) -> bool:
        return self.get_avg(tracker_id) >= threshold

    def clear(self) -> None:
        self.buffers.clear()

    def evict_lost(self, active_ids: set[int]) -> None:
        for tid in [t for t in self.buffers if t not in active_ids]:
            del self.buffers[tid]


class VivoGuardTracker:
    """Per-camera ByteTrack wrapper. One instance per camera, held in the
    inference worker's per-camera state (ByteTrack is stateful — never share
    across cameras). supervision is imported lazily so this module stays
    importable (for the Track dataclass / IOUTracker fallback) in envs
    without supervision installed."""

    def __init__(self, camera_id: int, *, frame_rate: int = INFERENCE_FPS):
        import supervision as sv
        self.camera_id = camera_id
        self.tracker = sv.ByteTrack(
            track_activation_threshold=BYTETRACK_ACTIVATION,
            lost_track_buffer=BYTETRACK_LOST_BUFFER,
            minimum_matching_threshold=BYTETRACK_MATCH_THRESH,
            frame_rate=frame_rate,
        )
        self.confidence_buffer = TrackConfidenceBuffer()
        # Persistent Track objects keyed by ByteTrack tracker_id (mirrors
        # IOUTracker.tracks) so history/extra survive frame-to-frame.
        self._tracks: dict[int, Track] = {}
        # A track lingers this long after ByteTrack stops reporting it,
        # matching lost_track_buffer at the configured frame rate.
        self._lost_seconds = BYTETRACK_LOST_BUFFER / max(1, frame_rate)

    def update(self, detections: list[dict]) -> list[tuple[Track, dict]]:
        """Adapter: dicts -> sv.Detections -> ByteTrack -> [(Track, det)].
        Reuses each ORIGINAL det dict (attaches track_id), never rebuilds it."""
        import numpy as np
        import supervision as sv
        now = time.time()

        if detections:
            xyxy = np.array([d["bbox_px"] for d in detections], dtype=float)
            conf = np.array([float(d.get("conf", 0.0)) for d in detections], dtype=float)
            cid  = np.array([int(d.get("cls_id", 0)) for d in detections], dtype=int)
            idx  = np.arange(len(detections))
            sv_dets = sv.Detections(xyxy=xyxy, confidence=conf, class_id=cid,
                                    data={"idx": idx})
        else:
            sv_dets = sv.Detections.empty()

        tracked = self.tracker.update_with_detections(sv_dets)

        out: list[tuple[Track, dict]] = []
        active_ids: set[int] = set()
        n = len(tracked)
        for i in range(n):
            if tracked.tracker_id is None:
                break
            tid = int(tracked.tracker_id[i])
            active_ids.add(tid)
            # Map back to the ORIGINAL detection dict (data['idx'] survives
            # ByteTrack); fall back to positional index if a version drops it.
            try:
                orig_i = int(tracked.data["idx"][i])
            except Exception:
                orig_i = i
            if orig_i < 0 or orig_i >= len(detections):
                continue
            det = detections[orig_i]
            det["track_id"] = tid                        # additive attach only
            conf_i = (float(tracked.confidence[i])
                      if tracked.confidence is not None else float(det.get("conf", 0.0)))
            self.confidence_buffer.update(tid, conf_i)

            tr = self._tracks.get(tid)
            if tr is None:
                tr = Track(track_id=tid, cls=det["cls"], bbox_norm=det["bbox_norm"],
                           first_seen=now, last_seen=now, history=[det["bbox_norm"]])
                self._tracks[tid] = tr
            else:
                tr.bbox_norm = det["bbox_norm"]
                tr.last_seen = now
                tr.history.append(det["bbox_norm"])
                if len(tr.history) > TRACK_HISTORY_LENGTH:
                    tr.history = tr.history[-TRACK_HISTORY_LENGTH:]
            # Track metadata lives ON the track (per review) so detectors read
            # tr.extra["rolling_conf"] without another object on the context.
            tr.extra["rolling_conf"] = self.confidence_buffer.get_avg(tid)
            tr.extra["frames_seen"]  = self.confidence_buffer.frames_seen(tid)
            tr.extra["stable"]       = self.confidence_buffer.is_confident(tid)
            out.append((tr, det))

        self._evict(active_ids, now)
        return out

    def _evict(self, active_ids: set[int], now: float) -> None:
        """Remove Track objects (and their confidence history) once ByteTrack
        has stopped reporting them for longer than lost_track_buffer."""
        expired = [tid for tid, tr in self._tracks.items()
                   if tid not in active_ids and (now - tr.last_seen) > self._lost_seconds]
        for tid in expired:
            self._tracks.pop(tid, None)
            self.confidence_buffer.buffers.pop(tid, None)

    def evict_lost(self, active_ids: set[int]) -> None:
        """Periodic (30s) cleanup hook for the inference worker."""
        self.confidence_buffer.evict_lost(active_ids)
        for tid in [t for t in self._tracks if t not in active_ids]:
            if (time.time() - self._tracks[tid].last_seen) > self._lost_seconds:
                self._tracks.pop(tid, None)

    def reset(self) -> None:
        """Full reset on stream (re)start — ByteTrack + all track/conf state."""
        try:
            self.tracker.reset()
        except Exception:
            pass
        self._tracks.clear()
        self.confidence_buffer.clear()
