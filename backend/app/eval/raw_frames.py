"""Clean (unannotated) alert frames for the evaluation test set.

Alert snapshots have boxes and a caption strip burned in, so detectors
cannot be scored on them. When settings.eval_capture_raw_frames is on,
the alert path also saves the untouched frame next to the snapshot as
`<snapshot stem>_raw.jpg` (deleted together with the snapshot by
maintenance.prune_alerts). Best-effort: never raises into the alert path.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

JPEG_QUALITY = 90


def raw_path_for(snapshot_path: str) -> str:
    p = Path(snapshot_path)
    return str(p.with_name(f"{p.stem}_raw.jpg"))


def save_raw_frame(frame_bgr, snapshot_path: str | None) -> str | None:
    """Write the clean frame beside its snapshot. Returns the path, or
    None (logged) when there is nothing to save or the write fails."""
    if not snapshot_path or frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return None
    try:
        import cv2
        out = raw_path_for(snapshot_path)
        if not cv2.imwrite(out, frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]):
            log.warning("eval: could not write clean frame %s", out)
            return None
        return out
    except Exception as exc:                                    # noqa: BLE001
        log.warning("eval: clean frame save failed for %s: %s", snapshot_path, exc)
        return None
