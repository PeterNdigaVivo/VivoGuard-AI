"""Business-hours alert filmstrip — 6 smartly-timed snapshots per alert.

When an operator-visible alert fires DURING business hours (08:00–20:00
EAT) we capture a 6-frame filmstrip that brackets the incident so the
Alerts page shows what led up to it AND what happened next:

    [-10s]  pre-event context (what led to the alert)
    [  0s]  the alert moment
    [+30s]  incident unfolding
    [+60s]  incident continuing
    [+90s]  incident continuing
    [+120s] resolution / aftermath

The timeline is per-type (FILMSTRIP_TIMELINES). shop_open_close overrides it
with the Yaya 9:13 door-opening sequence — [-30s][-15s][-5s][0s][+5s][+15s] —
so the strip shows the person approaching, opening the door, and entering.

Snapshot 1 (-10s): extracted from the camera's ACTIVE recording file at
(alert_time - 10s) when one is running; otherwise the most-recent Redis
frame (vg:frame:{cam}) is used as the best available "before" image.
Snapshot 2 is the Redis frame at t=0; snapshots 3-6 are captured by
`capture_filmstrip_frame` scheduled with Celery countdowns (30/60/90/120s).

Reuses Alert.snapshot_paths (a JSON list — the same column the checkout
and after-hours filmstrips use), so no schema or UI change. Files land under

    data/alert_snaps/{store_id}/{alert_id}/store{sid}_ts{epoch}.jpg

Everything is best-effort — a snapshot failure must never break alerting.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Filmstrip window: 08:00–20:00 EAT (20:00 exactly excluded). Starts an
# hour before official trading, matching the recorder — early store
# openings (08:0x) must get their strips too.
BUSINESS_START_HOUR = int(getattr(settings, "business_hours_start", 8))
BUSINESS_END_HOUR   = int(getattr(settings, "business_hours_end", 20))

# Alert types that get the 6-frame business-hours filmstrip. checkout_dwell
# is intentionally NOT here — it keeps its own bespoke timeline filmstrip
# (one frame per minute across the real dwell), which is richer than a
# generic -10..+120 strip.
FILMSTRIP_TYPES: set[str] = {
    "trespass", "intrusion", "staff_present", "staff_zone",
    "counter_unstaffed", "shop_open_close", "shrinkage", "uniform_compliance",
}

# Default timeline: 1 pre-frame (-10s), the t=0 frame, then +30/60/90/120s.
DEFAULT_PRE_OFFSETS_S  = (10,)                 # snapshot 1 — before the alert
DEFAULT_POST_OFFSETS_S = (30, 60, 90, 120)     # snapshots 3-6 — after the alert

# Per-type overrides. shop_open_close uses the tight door-opening timeline
# from the Yaya 9:13 store-open: -30/-15/-5 (approach), t=0 (crossing),
# +5/+15 (door open + entering). Pre offsets are POSITIVE seconds before the
# alert; each pair totals <= MAX_SNAPSHOTS frames counting the t=0 frame.
FILMSTRIP_TIMELINES: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "shop_open_close": ((30, 15, 5), (5, 15)),
}
MAX_SNAPSHOTS  = 6

# 48h retention for the on-disk filmstrips (mirrors prune_alert_clips).
RETENTION_HOURS = int(getattr(settings, "alert_snapshot_retention_hours", 48))


# ── time / gating ─────────────────────────────────────────────────────────
def _eat(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Africa/Nairobi"))


def _within_business_hours(dt: datetime) -> bool:
    hour = _eat(dt).hour
    return BUSINESS_START_HOUR <= hour < BUSINESS_END_HOUR


# ── storage ───────────────────────────────────────────────────────────────
def _root() -> Path:
    return Path(settings.recordings_dir) / "alert_snaps"


def _alert_dir(store_id: int, alert_id: int) -> Path:
    return _root() / str(store_id) / str(alert_id)


def _save_jpeg(jpeg: bytes, *, store_id: int, alert_id: int, epoch_ts: int) -> str | None:
    """Persist an already-encoded JPEG into the alert folder. Best-effort."""
    try:
        if not jpeg:
            return None
        d = _alert_dir(store_id, alert_id)
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"store{store_id}_ts{epoch_ts}.jpg"
        out.write_bytes(jpeg)
        return str(out)
    except Exception as e:
        log.warning("alert filmstrip save failed store=%s alert=%s: %s",
                    store_id, alert_id, e)
        return None


def _append_path(alert_id: int, path: str) -> None:
    """Append one snapshot path to Alert.snapshot_paths. Reassigns the list
    (SQLAlchemy JSON does not track in-place mutation) and caps at 6."""
    from app.database import SessionLocal
    from app.models import Alert
    try:
        with SessionLocal() as db:
            a = db.get(Alert, alert_id)
            if not a:
                return
            paths = list(a.snapshot_paths or [])
            if len(paths) >= MAX_SNAPSHOTS or path in paths:
                return
            paths.append(path)
            a.snapshot_paths = paths
            db.commit()
    except Exception as e:
        log.warning("alert filmstrip append failed alert=%s: %s", alert_id, e)


# ── frame sources ─────────────────────────────────────────────────────────
def _redis_frame(camera_id: int) -> bytes | None:
    """Most-recent JPEG the streamer published (raw pixels, no overlay)."""
    try:
        from app.stream.frame_buffer import FrameBuffer
        return FrameBuffer().latest_jpeg(camera_id, prefer_overlay=False)
    except Exception:
        return None


def _extract_recording_frame(camera_id: int, target_dt: datetime) -> bytes | None:
    """Pull a single JPEG at `target_dt` from the camera's ACTIVE recording
    window, if one is running. Returns None when there's no live recording
    or ffmpeg fails (caller falls back to the Redis frame)."""
    from app.database import SessionLocal
    from app.models import RecordingClip
    try:
        with SessionLocal() as db:
            clip = (db.query(RecordingClip)
                      .filter(RecordingClip.camera_id == camera_id,
                              RecordingClip.status == "recording",
                              RecordingClip.started_at <= target_dt)
                      .order_by(RecordingClip.started_at.desc())
                      .first())
            if not clip or not clip.file_path or not Path(clip.file_path).exists():
                return None
            started = clip.started_at
            file_path = clip.file_path
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        offset = max(0, int((target_dt - started).total_seconds()))
        out = Path(tempfile.gettempdir()) / f"vg_pre_{camera_id}_{int(target_dt.timestamp())}.jpg"
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
               "-ss", str(offset), "-i", file_path,
               "-frames:v", "1", "-q:v", "3", str(out)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        if out.exists():
            data = out.read_bytes()
            try: out.unlink()
            except OSError: pass
            return data or None
    except Exception as e:
        log.warning("alert filmstrip pre-frame extract failed cam=%s: %s",
                    camera_id, e)
    return None


# ── celery tasks ──────────────────────────────────────────────────────────
@celery_app.task(name="alerting.capture_filmstrip_frame", ignore_result=True)
def capture_filmstrip_frame(alert_id: int, camera_id: int, store_id: int,
                            epoch_ts: int) -> None:
    """Capture ONE filmstrip frame from the current Redis frame and append it
    to the alert. Used for the +30/+60/+90/+120s snapshots (scheduled with a
    countdown). Best-effort."""
    jpeg = _redis_frame(camera_id)
    if not jpeg:
        return
    path = _save_jpeg(jpeg, store_id=store_id, alert_id=alert_id, epoch_ts=epoch_ts)
    if path:
        _append_path(alert_id, path)


@celery_app.task(name="alerting.schedule_alert_filmstrip", ignore_result=True)
def schedule_alert_filmstrip(alert_id: int, camera_id: int, store_id: int,
                             detection_type: str, alert_epoch: float) -> None:
    """Kick off the 6-frame filmstrip for a business-hours alert: capture the
    pre-frames and t=0 frame now, then schedule the post-frames via countdown.
    Timeline is per-type (FILMSTRIP_TIMELINES) — the default is -10s/t=0/
    +30/60/90/120s; shop_open_close uses -30/-15/-5/t=0/+5/+15 to capture the
    approach-to-door + door-opening motion. Gated on the alert type and the
    09:00–20:00 EAT window (evaluated at the alert's own time). Best-effort —
    never raises."""
    if not bool(getattr(settings, "business_hours_snapshot_enabled", True)):
        return
    if detection_type not in FILMSTRIP_TYPES:
        return
    alert_dt = datetime.fromtimestamp(alert_epoch, tz=timezone.utc)
    if not _within_business_hours(alert_dt):
        return

    pre_offsets, post_offsets = FILMSTRIP_TIMELINES.get(
        detection_type, (DEFAULT_PRE_OFFSETS_S, DEFAULT_POST_OFFSETS_S))

    # Pre-frames — each extracted from the active recording at (alert - Ns),
    # else the most-recent Redis frame. Oldest-first so the strip reads
    # left-to-right as the approach to the door.
    for pre in sorted(pre_offsets, reverse=True):    # e.g. 30,15,5 → -30/-15/-5s
        pre_dt = alert_dt - timedelta(seconds=pre)
        pre_jpeg = _extract_recording_frame(camera_id, pre_dt) or _redis_frame(camera_id)
        if pre_jpeg:
            p = _save_jpeg(pre_jpeg, store_id=store_id, alert_id=alert_id,
                           epoch_ts=int(pre_dt.timestamp()))
            if p:
                _append_path(alert_id, p)

    # t=0 frame — the alert moment (most-recent Redis frame).
    now_jpeg = _redis_frame(camera_id)
    if now_jpeg:
        p = _save_jpeg(now_jpeg, store_id=store_id, alert_id=alert_id,
                       epoch_ts=int(alert_dt.timestamp()))
        if p:
            _append_path(alert_id, p)

    # Post-frames via Celery countdown (grab the live Redis frame at +Ns).
    base = int(alert_dt.timestamp())
    for off in post_offsets:
        capture_filmstrip_frame.apply_async(
            args=[alert_id, camera_id, store_id, base + off],
            countdown=off,
        )


# ── retention ─────────────────────────────────────────────────────────────
@celery_app.task(name="alerting.prune_alert_snapshots", ignore_result=True)
def prune_alert_snapshots() -> None:
    """Hourly: delete filmstrip JPEGs under data/alert_snaps older than the
    retention window (default 48h) and remove the now-empty {store}/{alert}
    folders. Mirrors prune_alert_clips / prune_checkout_snapshots.

    Filesystem-driven (by file mtime) rather than DB-driven: the checkout
    pruner nulls Alert.snapshot_paths at 24h, so a query filtered on
    snapshot_paths would miss these files and leak them. Best-effort."""
    root = _root()
    if not root.exists():
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=RETENTION_HOURS)).timestamp()
    deleted = 0
    try:
        for store_dir in root.iterdir():
            if not store_dir.is_dir():
                continue
            for alert_dir in store_dir.iterdir():
                if not alert_dir.is_dir():
                    continue
                for f in alert_dir.iterdir():
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff:
                            f.unlink()
                            deleted += 1
                    except Exception:
                        pass
                # Remove the alert folder once it's empty.
                try:
                    if not any(alert_dir.iterdir()):
                        alert_dir.rmdir()
                except OSError:
                    pass
            # Remove the store folder once it's empty.
            try:
                if not any(store_dir.iterdir()):
                    store_dir.rmdir()
            except OSError:
                pass
    except Exception as e:
        log.warning("prune_alert_snapshots failed: %s", e)
    if deleted:
        log.info("prune_alert_snapshots: deleted %d files older than %dh",
                 deleted, RETENTION_HOURS)
