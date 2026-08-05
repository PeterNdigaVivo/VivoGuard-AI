"""Tests for app.ai.tracker.is_static_track — the mannequin filter.

A mannequin's tracked bbox never moves; a real person's does. Judged on
the track's own bbox-centre history in PIXELS (max displacement vs the
window's first position).
"""
from __future__ import annotations

from app.ai.tracker import is_static_track

FRAME = (640, 360)          # (w, h)


def _bb(cx: float, cy: float, w: float = 0.05, h: float = 0.15) -> list[float]:
    """Normalised [x1,y1,x2,y2] around a centre point (also normalised)."""
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def test_mannequin_static_bbox_is_static() -> None:
    hist = [_bb(0.5, 0.5)] * 10
    assert is_static_track(hist, FRAME) is True


def test_subpixel_jitter_still_static() -> None:
    # ±1px of detector jitter at 640px wide ≈ ±0.0016 normalised.
    hist = [_bb(0.5 + (0.0015 if i % 2 else -0.0015), 0.5)
            for i in range(10)]
    assert is_static_track(hist, FRAME) is True


def test_walking_person_is_moving() -> None:
    # Drifts 0.5 -> 0.59 across the window ≈ 57px at 640 wide.
    hist = [_bb(0.5 + i * 0.01, 0.5) for i in range(10)]
    assert is_static_track(hist, FRAME) is False


def test_pacing_person_returning_to_start_is_moving() -> None:
    # Out and back: end-to-end displacement ~0, but max-vs-first is large.
    xs = [0.5, 0.52, 0.55, 0.58, 0.60, 0.58, 0.55, 0.52, 0.50, 0.50]
    hist = [_bb(x, 0.5) for x in xs]
    assert is_static_track(hist, FRAME) is False


def test_young_track_never_judged_static() -> None:
    # Fewer than `window` frames → a person who just appeared is NEVER
    # suppressed; mannequins accumulate the window within seconds anyway.
    hist = [_bb(0.5, 0.5)] * 9
    assert is_static_track(hist, FRAME, window=10) is False
    assert is_static_track([], FRAME) is False


def test_threshold_boundary() -> None:
    # Exactly min_px of movement counts as MOVING (>= is not static).
    hist = [_bb(0.5, 0.5)] * 9 + [_bb(0.5 + 5.0 / 640, 0.5)]
    assert is_static_track(hist, FRAME, min_px=5.0) is False
    hist = [_bb(0.5, 0.5)] * 9 + [_bb(0.5 + 4.0 / 640, 0.5)]
    assert is_static_track(hist, FRAME, min_px=5.0) is True


def test_only_last_window_considered() -> None:
    # Moved long ago, frozen for the last `window` frames → static now.
    hist = ([_bb(0.2 + i * 0.05, 0.5) for i in range(6)]
            + [_bb(0.5, 0.5)] * 10)
    assert is_static_track(hist, FRAME, window=10) is True
