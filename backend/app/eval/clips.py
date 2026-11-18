"""People counts on the hand-counted clips, through the live pipeline.

Each clip was labelled with the number of DIFFERENT people in it. The
model's count is the number of distinct person tracks, produced the way
live inference produces them: the same detector, the same tracker
(ByteTrack, IOU fallback) at the live frame rate, and the same static-
object (mannequin) filter. Two counts are reported per clip:

  raw       every person track the tracker kept
  filtered  minus tracks the live mannequin filter treats as fixtures

This is the "real-world" number the still-frame scores cannot give: the
mannequin filter needs movement over several frames to work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

import numpy as np

log = logging.getLogger(__name__)

# A track seen in a single frame is detector flicker, not a person who
# walked through; live alerts gate on several frames too.
MIN_TRACK_FRAMES = 2


@dataclass
class ClipCount:
    raw: int
    filtered: int
    frames: int


def iter_clip_frames(path: str, fps: float) -> Iterator[np.ndarray]:
    """Frames of a clip sampled at `fps` (the live inference rate)."""
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise OSError(f"cannot open clip {path}")
    try:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        step = max(1, int(round(src_fps / fps))) if src_fps > 0 else 1
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                yield frame
            idx += 1
    finally:
        cap.release()


def make_tracker(fps: float):
    """The live tracker: ByteTrack via supervision, IOUTracker otherwise."""
    try:
        from app.ai.tracker import VivoGuardTracker
        return VivoGuardTracker(0, frame_rate=max(1, int(round(fps))))
    except Exception as exc:                                    # noqa: BLE001
        from app.ai.tracker import IOUTracker
        log.warning("eval clips: ByteTrack unavailable (%s) — IOUTracker", exc)
        return IOUTracker()


def count_people(frames: Iterable[np.ndarray], infer_fn: Callable, tracker, *,
                 filter_enabled: bool = True, min_px: float = 5.0,
                 window: int = 10) -> ClipCount:
    from app.ai.tracker import partition_static_person_tracks
    seen: dict[int, int] = {}            # person track id → frames seen
    static_now: dict[int, bool] = {}     # mannequin verdict at last sighting
    n = 0
    for frame in frames:
        n += 1
        raw = infer_fn(frame)
        tracks = tracker.update(raw)
        h, w = frame.shape[:2]
        _, _, static_ids = partition_static_person_tracks(
            raw, tracks, (w, h), enabled=filter_enabled, min_px=min_px, window=window)
        for track, det in tracks:
            if det.get("cls") != "person":
                continue
            seen[track.track_id] = seen.get(track.track_id, 0) + 1
            static_now[track.track_id] = track.track_id in static_ids
    people = [tid for tid, k in seen.items() if k >= MIN_TRACK_FRAMES]
    return ClipCount(raw=len(people),
                     filtered=sum(1 for tid in people if not static_now.get(tid)),
                     frames=n)


def evaluate_clips(samples: list, infer_fn: Callable, *, fps: float,
                   frames_fn: Callable[[str, float], Iterable] = iter_clip_frames,
                   tracker_fn: Callable[[float], object] = make_tracker) -> dict:
    """Score every labelled clip; returns raw and filtered count accuracy."""
    from app.config import settings
    from app.eval import scoring
    raw_pairs: list[tuple[int, int]] = []
    filtered_pairs: list[tuple[int, int]] = []
    errors = 0
    for s in samples:
        try:
            got = count_people(
                frames_fn(s.clip_path, fps), infer_fn, tracker_fn(fps),
                filter_enabled=bool(getattr(settings, "mannequin_filter_enabled", True)),
                min_px=float(getattr(settings, "activity_static_displacement_px", 5)),
                window=int(getattr(settings, "activity_static_window_frames", 10)))
        except Exception as exc:                                # noqa: BLE001
            errors += 1
            log.warning("eval: clip sample %s failed: %s", s.id, exc)
            continue
        hand = int(s.label["count"])
        raw_pairs.append((got.raw, hand))
        filtered_pairs.append((got.filtered, hand))
    return {"fps": fps, "raw": scoring.summarise_counts(raw_pairs),
            "filtered": scoring.summarise_counts(filtered_pairs), "errors": errors}
