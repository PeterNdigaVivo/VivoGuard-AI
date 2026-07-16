"""Snapshot filmstrip for after-hours intrusion alerts.

The `alerting.after_hours_intrusion_check` beat task creates ONE intrusion
alert per store per after-hours window and captures up to 6 snapshots (the
first immediately, then one every 5 min) from the best available store
camera. Files land in

    data/after_hours_snaps/{store_id}/{alert_id}/store{sid}_ts{epoch}.jpg

— a SEPARATE root from `checkout_snaps`, so the two filmstrips never
collide. The JPEGs are the already-encoded frames pulled straight from the
Redis frame buffer, so no cv2 re-encode is needed here.

24h retention via `alerting.after_hours_prune`. Every disk op is
best-effort — a snapshot failure must never break the alerting beat loop.
"""
from __future__ import annotations
import logging
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


def _root() -> Path:
    return Path(settings.recordings_dir) / "after_hours_snaps"


def _alert_dir(store_id: int, alert_id: int) -> Path:
    return _root() / str(store_id) / str(alert_id)


def save_snapshot(jpeg: bytes, *, store_id: int, alert_id: int,
                  epoch_ts: int) -> str | None:
    """Persist an already-encoded JPEG into the alert folder. Returns the
    absolute path on success, None on any failure."""
    try:
        if not jpeg:
            return None
        d = _alert_dir(store_id, alert_id)
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"store{store_id}_ts{epoch_ts}.jpg"
        out.write_bytes(jpeg)
        return str(out)
    except Exception as e:
        log.warning("after-hours snapshot save failed store=%s alert=%s: %s",
                    store_id, alert_id, e)
        return None


def delete_alert_folder(store_id: int, alert_id: int) -> int:
    """Called by the 24h pruner. Returns count of files deleted. Best-effort
    — a missing folder returns 0."""
    d = _alert_dir(store_id, alert_id)
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
        # Clean the now-empty store dir too (best-effort).
        try: d.parent.rmdir()
        except OSError: pass
    except Exception as e:
        log.warning("after-hours prune folder failed store=%s alert=%s: %s",
                    store_id, alert_id, e)
    return n
