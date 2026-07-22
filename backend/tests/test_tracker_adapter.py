"""Compatibility safety-net for the ByteTrack adapter (VivoGuardTracker).

This is the test the migration plan requires BEFORE wiring the adapter into
the inference worker (P3): it proves VivoGuardTracker.update() returns the
exact same list[(Track, det)] contract the ~25 detectors rely on today from
IOUTracker, so swapping the tracker can't silently break the detector layer.

Skips cleanly when supervision isn't installed (e.g. the API-only test image);
runs on the worker/CI image where supervision==0.22.0 is present.
"""
from __future__ import annotations
import pytest

pytest.importorskip("supervision")
pytest.importorskip("numpy")

from app.ai.tracker import Track, VivoGuardTracker  # noqa: E402


def _fake_dets():
    """Three person detections in a 640x360 frame — the shape infer() emits
    (plus the additive cls_id the adapter needs to build sv.Detections)."""
    w, h = 640, 360
    boxes_px = [[100, 80, 160, 300], [300, 90, 360, 310], [500, 70, 560, 300]]
    dets = []
    for bp in boxes_px:
        x1, y1, x2, y2 = bp
        dets.append({
            "cls": "person", "cls_id": 0, "conf": 0.9,
            "bbox_px": [float(x1), float(y1), float(x2), float(y2)],
            "bbox_norm": [x1 / w, y1 / h, x2 / w, y2 / h],
        })
    return dets


def test_update_returns_track_det_contract():
    tr = VivoGuardTracker(camera_id=1)
    # ByteTrack needs a couple of frames to promote detections to tracks.
    out = []
    for _ in range(3):
        out = tr.update(_fake_dets())

    assert isinstance(out, list)
    assert out, "expected active tracks after a few frames"
    for track, det in out:
        # Track side of the contract (what detectors read).
        assert isinstance(track, Track)
        assert isinstance(track.track_id, int)
        assert isinstance(track.history, list) and track.history
        assert track.first_seen <= track.last_seen
        assert len(track.bbox_norm) == 4
        # Adapter metadata on the track (per review).
        assert "rolling_conf" in track.extra
        assert "frames_seen" in track.extra
        # det side of the contract — ORIGINAL dict, reused verbatim.
        assert det["cls"] == "person"
        assert len(det["bbox_px"]) == 4
        assert len(det["bbox_norm"]) == 4
        assert 0.0 <= det["conf"] <= 1.0
        assert det["track_id"] == track.track_id   # id attached, not rebuilt


def test_ids_persist_across_frames():
    tr = VivoGuardTracker(camera_id=2)
    ids_seen = []
    for _ in range(4):
        out = tr.update(_fake_dets())
        ids_seen.append({t.track_id for t, _ in out})
    # Once promoted, the same ids should recur (persistent tracking).
    assert ids_seen[-1] and ids_seen[-1] == ids_seen[-2]


def test_reset_clears_state():
    tr = VivoGuardTracker(camera_id=3)
    for _ in range(3):
        tr.update(_fake_dets())
    assert tr._tracks
    tr.reset()
    assert tr._tracks == {}
    assert tr.confidence_buffer.buffers == {}


def test_history_capped():
    from app.ai.tracker import TRACK_HISTORY_LENGTH
    tr = VivoGuardTracker(camera_id=4)
    for _ in range(TRACK_HISTORY_LENGTH + 20):
        tr.update(_fake_dets())
    for track in tr._tracks.values():
        assert len(track.history) <= TRACK_HISTORY_LENGTH
