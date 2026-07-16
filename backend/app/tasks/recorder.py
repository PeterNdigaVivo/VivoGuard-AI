"""Rolling recording system — runs in the dedicated `recorder` compose
service (celery worker -Q recorder), so it survives worker/inference
rebuilds. ffmpeg subprocesses are children of THIS container.

Windows (EAT), business hours only:
    09:00-14:00  window "<date>_0900"  (18000s)
    14:00-19:00  window "<date>_1400"  (18000s)
    19:00-20:00  window "<date>_1900"  ( 3600s)
Outside 09:00-20:00 EAT: nothing records.

At each transition the PREVIOUS window's directory is deleted. Recording is
substream, stream-copy (no re-encode), fragmented-mp4 so an in-progress file
stays seekable for alert-clip extraction:
    ffmpeg -rtsp_transport tcp -i <sub_url> -t <secs> -c copy \
           -movflags +frag_keyframe+empty_moov -y out.mp4

Scheduling uses an interval TICK + EAT-clock gate (NOT crontab): the beat
worker fires recorder.tick every 60s; window start/end/delete is driven off
the EAT wall clock, so it is correct regardless of APP_TIMEZONE.

RTSP URLs contain decrypted camera passwords — they are NEVER logged.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import redis

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Ordered windows: (start_hour, end_hour, suffix, seconds).
_WINDOWS = [(9, 14, "0900", 18000), (14, 19, "1400", 18000), (19, 20, "1900", 3600)]

_PID_KEY_FMT = "vg:recording:pid:{cam}"          # → json {pid, window_id, path}
_CURRENT_WINDOW_KEY = "vg:recording:current_window"

# Alert-clip segment durations (seconds) per detection type.
_CLIP_DURATIONS = {
    "shop_open_close": 180,
    "intrusion": 120, "trespass": 120, "shrinkage": 120, "after_hours": 120,
    "staff_present": 60,
    # checkout_dwell is computed from dwell_seconds + 60 (see _clip_duration).
}
_CLIP_DEFAULT = 90


def _redis():
    return redis.from_url(settings.redis_url, decode_responses=True)


def _eat_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Nairobi"))


def _clips_root() -> Path:
    return Path(settings.recordings_dir) / "clips"


def _alert_clips_root() -> Path:
    return Path(settings.recordings_dir) / "alert_clips"


def _current_window(now_eat: datetime):
    """Return (window_id, seconds, window_start_eat) for the active window, or
    None outside business hours."""
    h = now_eat.hour
    for start_h, end_h, suffix, secs in _WINDOWS:
        if start_h <= h < end_h:
            wid = f"{now_eat.strftime('%Y%m%d')}_{suffix}"
            start = now_eat.replace(hour=start_h, minute=0, second=0, microsecond=0)
            return wid, secs, start
    return None


def _key_cameras(db):
    """KEY cameras = those carrying a counter / entry_exit / staff_zone zone."""
    from app.models import Camera, Zone
    tags = {"counter", "entry_exit", "staff_zone"}
    cam_ids = {
        z.camera_id for z in db.query(Zone.camera_id, Zone.detection_types_json).all()
        if tags & set(z.detection_types_json or [])
    }
    if not cam_ids:
        return []
    return db.query(Camera).filter(Camera.id.in_(cam_ids)).all()


def _substream_url(cam) -> str | None:
    """Build the substream RTSP URL for a camera (decrypted creds). Returns
    None if the camera lacks a host. NEVER log the return value."""
    try:
        from app.utils.crypto import decrypt
        from app.utils.network import build_rtsp_url
        if not cam.host:
            return None
        pw = decrypt(cam.password_encrypted or "") if cam.password_encrypted else ""
        return build_rtsp_url(
            brand=cam.brand, host=cam.host, port=cam.rtsp_port,
            username=cam.username, password=pw,
            channel=cam.channel_number, subtype=1,
            override=cam.rtsp_url_override,
        )
    except Exception as e:
        log.warning("recorder: could not build RTSP url for cam=%s: %s", cam.id, e)
        return None


def _start_window(db, r, window_id: str, seconds: int) -> int:
    """Spawn one stream-copy ffmpeg per key camera. Returns count started."""
    from app.models import RecordingClip
    started = 0
    for cam in _key_cameras(db):
        url = _substream_url(cam)
        if not url:
            continue
        out_dir = _clips_root() / window_id / str(cam.store_id or 0)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{cam.id}.mp4"
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", url,
            "-t", str(seconds), "-c", "copy",
            "-movflags", "+frag_keyframe+empty_moov",
            "-y", str(out_path),
        ]
        try:
            # Detached so it isn't reaped when the tick task returns; it lives
            # for the window (or until -t / SIGTERM). If the camera refuses a
            # 2nd connection, ffmpeg exits quickly and the file stays tiny —
            # inference always keeps priority (we never touch the streamer).
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            log.warning("recorder: ffmpeg spawn failed cam=%s: %s", cam.id, e)
            continue
        r.set(_PID_KEY_FMT.format(cam=cam.id),
              json.dumps({"pid": proc.pid, "window_id": window_id, "path": str(out_path)}),
              ex=6 * 3600)
        db.add(RecordingClip(camera_id=cam.id, store_id=cam.store_id,
                             window_id=window_id, file_path=str(out_path),
                             status="recording"))
        started += 1
    db.commit()
    log.info("Started recording %d cameras for window %s", started, window_id)
    return started


def _stop_all(r) -> None:
    """SIGTERM every tracked ffmpeg, then SIGKILL survivors after a grace."""
    pids: list[int] = []
    for key in r.scan_iter(match="vg:recording:pid:*", count=200):
        try:
            pid = int(json.loads(r.get(key) or "{}").get("pid") or 0)
        except Exception:
            pid = 0
        if pid:
            pids.append(pid)
        r.delete(key)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        except Exception: pass
    if pids:
        time.sleep(3)   # brief grace; most self-terminated at -t already
    for pid in pids:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        except Exception: pass


def _delete_window(db, window_id: str) -> None:
    """Delete a completed window's directory + mark its rows deleted."""
    from app.models import RecordingClip
    d = _clips_root() / window_id
    freed = 0
    if d.exists():
        for f in d.rglob("*"):
            try: freed += f.stat().st_size
            except Exception: pass
        shutil.rmtree(d, ignore_errors=True)
    now = datetime.now(timezone.utc)
    (db.query(RecordingClip)
       .filter(RecordingClip.window_id == window_id,
               RecordingClip.status != "deleted")
       .update({"status": "deleted", "file_path": None, "ended_at": now},
               synchronize_session=False))
    db.commit()
    log.info("Deleted recording window %s — freed %.1f GB", window_id, freed / 1024**3)


@celery_app.task(name="recorder.tick", ignore_result=True)
def tick() -> None:
    """Every 60s: drive window start/stop/delete off the EAT wall clock."""
    if not bool(getattr(settings, "recording_enabled", True)):
        return
    from app.database import SessionLocal
    r = _redis()
    now_eat = _eat_now()
    win = _current_window(now_eat)
    prev = r.get(_CURRENT_WINDOW_KEY)

    # Outside business hours → ensure everything is stopped + purged.
    if win is None:
        if prev:
            _stop_all(r)
            with SessionLocal() as db:
                _delete_window(db, prev)
            r.delete(_CURRENT_WINDOW_KEY)
        return

    window_id, seconds, _start = win
    if prev == window_id:
        return   # already recording this window — nothing to do
    # Transition: stop + delete previous window, start the new one.
    _stop_all(r)
    with SessionLocal() as db:
        if prev:
            _delete_window(db, prev)
        _start_window(db, r, window_id, seconds)
    r.set(_CURRENT_WINDOW_KEY, window_id, ex=6 * 3600)


# ── Alert clip extraction ─────────────────────────────────────────────────
def _clip_duration(detection_type: str, extra: dict) -> int:
    if detection_type == "checkout_dwell":
        return int((extra or {}).get("dwell_seconds") or 60) + 60
    return _CLIP_DURATIONS.get(detection_type or "", _CLIP_DEFAULT)


@celery_app.task(name="recorder.extract_pending_clips", ignore_result=True)
def extract_pending_clips() -> None:
    """Every 60s: for recent alerts of clip-worthy types that don't yet have a
    clip, cut the relevant segment from that camera's active window recording.
    Best-effort — a failure just leaves the alert with its snapshot."""
    if not bool(getattr(settings, "recording_enabled", True)):
        return
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent, RecordingClip
    from app.tasks.recorder import _clips_root  # noqa: F401 (self, keeps root consistent)
    r = _redis()
    cur_window = r.get(_CURRENT_WINDOW_KEY)
    if not cur_window:
        return
    win = _current_window(_eat_now())
    if not win:
        return
    _wid, _secs, window_start_eat = win
    window_start_utc = window_start_eat.astimezone(timezone.utc)
    targets = set(_CLIP_DURATIONS) | {"checkout_dwell"}
    since = datetime.now(timezone.utc) - timedelta(minutes=3)
    _alert_clips_root().mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        rows = (db.query(Alert, DetectionEvent)
                  .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                  .filter(DetectionEvent.timestamp >= since,
                          DetectionEvent.detection_type.in_(targets))
                  .all())
        for alert, ev in rows:
            if (ev.extra or {}).get("alert_clip_path"):
                continue
            clip = (db.query(RecordingClip)
                      .filter(RecordingClip.camera_id == ev.camera_id,
                              RecordingClip.window_id == cur_window,
                              RecordingClip.status == "recording")
                      .first())
            if clip is None or not clip.file_path or not Path(clip.file_path).exists():
                continue
            offset = max(0, int((ev.timestamp.astimezone(timezone.utc)
                                 - window_start_utc).total_seconds()) - 5)
            dur = _clip_duration(ev.detection_type, ev.extra or {})
            out = _alert_clips_root() / f"{alert.id}.mp4"
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                   "-ss", str(offset), "-i", clip.file_path,
                   "-t", str(dur), "-c", "copy", str(out)]
            try:
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=30)
            except Exception as e:
                log.warning("recorder: clip extract failed alert=%s: %s", alert.id, e)
                continue
            if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
                ev.extra = {**(ev.extra or {}), "alert_clip_path": str(out)}
                db.commit()
                log.info("recorder: extracted clip alert=%s cam=%s dur=%ds",
                         alert.id, ev.camera_id, dur)


@celery_app.task(name="recorder.prune_alert_clips", ignore_result=True)
def prune_alert_clips() -> None:
    """Delete alert clips older than the retention window (default 48h) and
    clear their extra path."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent
    hours = int(getattr(settings, "recording_alert_clip_retention_hours", 48))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cleared = 0
    with SessionLocal() as db:
        rows = (db.query(Alert, DetectionEvent)
                  .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                  .filter(Alert.created_at < cutoff)
                  .all())
        for _alert, ev in rows:
            p = (ev.extra or {}).get("alert_clip_path")
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
            ev.extra = {k: v for k, v in (ev.extra or {}).items() if k != "alert_clip_path"}
            cleared += 1
        if cleared:
            db.commit()
            log.info("recorder: pruned %d alert clips", cleared)


# ── Storage health ─────────────────────────────────────────────────────────
def _dir_size_bytes(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try: total += f.stat().st_size
        except Exception: pass
    return total


@celery_app.task(name="recorder.storage_health_check", ignore_result=True)
def storage_health_check() -> None:
    """Every 30min: warn/urgent-alert on recording-storage usage."""
    used_gb = _dir_size_bytes(_clips_root()) / 1024**3
    used_gb += _dir_size_bytes(_alert_clips_root()) / 1024**3
    warn = int(getattr(settings, "recording_max_used_gb_warning", 550))
    crit = int(getattr(settings, "recording_max_used_gb_critical", 580))
    r = _redis()
    if used_gb >= crit:
        _storage_alert(r, "URGENT", f"Recording storage critical: {used_gb:.0f} GB used (>{crit} GB)")
    elif used_gb >= warn:
        _storage_alert(r, "WARNING", f"Recording storage high: {used_gb:.0f} GB used (>{warn} GB)")
    else:
        log.info("recorder: storage healthy — %.0f GB used", used_gb)


def _storage_alert(r, level: str, body: str) -> None:
    """Dedup one storage alert per level per hour → ops channel + log."""
    key = f"vg:recording:storage_alert:{level}"
    try:
        if r.get(key):
            return
        r.set(key, "1", ex=3600)
    except Exception:
        pass
    try:
        from app.tasks.alerting import _dashboard_recipients
        from app.tasks.briefings import _send_whatsapp
        _send_whatsapp(_dashboard_recipients(), f"[VivoGuard recorder] {level}: {body}")
    except Exception:
        pass
    log.warning("recorder storage %s: %s", level, body)
