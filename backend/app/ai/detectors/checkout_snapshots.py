"""Per-session snapshot capture for checkout_dwell sessions.

The detector calls capture_pending() every 60s during an open
session. Files land in
  data/checkout_snaps/pending/cam{cid}_tr{tid}_zn{zid}/cam{cid}_ts{epoch}.jpg

On alert fire, promote_to_alert() moves them to
  data/checkout_snaps/alerts/{alert_id}/

On session close WITHOUT alert: discard_pending() deletes the
pending folder.

On session close AFTER alert: promote_to_alert() (re-called) moves
any remaining pending frames into the alert folder and returns the
NEW path list — caller dedups/appends against Alert.snapshot_paths.

All disk operations are best-effort — a snapshot failure must
never block the detector's per-frame loop.
"""
from __future__ import annotations
import logging
import shutil
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# 60-second cadence — the spec. Constant rather than magic number.
SNAPSHOT_INTERVAL_S = 60.0


def _root() -> Path:
    return Path(settings.recordings_dir) / "checkout_snaps"


def _pending_dir(camera_id: int, track_id: int, zone_id: int) -> Path:
    return _root() / "pending" / f"cam{camera_id}_tr{track_id}_zn{zone_id}"


def _alert_dir(alert_id: int) -> Path:
    return _root() / "alerts" / str(alert_id)


def capture_pending(frame_bgr, *, camera_id: int, track_id: int,
                    zone_id: int, epoch_ts: int) -> Optional[str]:
    """Write the current frame to the pending folder. Returns the
    absolute path on success, None on any failure."""
    try:
        import cv2
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return None
        d = _pending_dir(camera_id, track_id, zone_id)
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"cam{camera_id}_ts{epoch_ts}.jpg"
        if not cv2.imwrite(str(out), frame_bgr):
            return None
        return str(out)
    except Exception as e:
        log.warning("checkout snapshot failed cam=%s track=%s zone=%s: %s",
                    camera_id, track_id, zone_id, e)
        return None


def promote_to_alert(*, camera_id: int, track_id: int, zone_id: int,
                     alert_id: int) -> list[str]:
    """Move all pending JPEGs for this session into the alert folder.
    Returns the sorted list of new permanent paths (alphabetical =
    chronological since timestamps are in filenames). Best-effort
    throughout — a missing pending folder returns []."""
    src = _pending_dir(camera_id, track_id, zone_id)
    if not src.exists():
        return []
    dst = _alert_dir(alert_id)
    dst.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        for f in sorted(src.iterdir()):
            if f.suffix.lower() == ".jpg":
                target = dst / f.name
                try:
                    shutil.move(str(f), str(target))
                    moved.append(str(target))
                except Exception:
                    log.exception("promote snapshot failed: %s → %s", f, target)
        # Best-effort cleanup of the now-empty pending dir.
        try: src.rmdir()
        except OSError: pass
    except Exception as e:
        log.exception("promote_to_alert failed cam=%s track=%s alert=%s: %s",
                      camera_id, track_id, alert_id, e)
    return moved


def discard_pending(camera_id: int, track_id: int, zone_id: int) -> None:
    """Session closed without alert. Delete the pending folder."""
    d = _pending_dir(camera_id, track_id, zone_id)
    if d.exists():
        try: shutil.rmtree(d, ignore_errors=True)
        except Exception: pass


def delete_alert_folder(alert_id: int) -> int:
    """Called by the 24h pruner. Returns count of files deleted.
    Best-effort — missing folder returns 0."""
    d = _alert_dir(alert_id)
    if not d.exists():
        return 0
    n = 0
    try:
        for f in d.iterdir():
            try:
                f.unlink(); n += 1
            except Exception:
                pass
        try: d.rmdir()
        except OSError: pass
    except Exception as e:
        log.warning("prune snapshot folder failed alert=%s: %s", alert_id, e)
    return n
