"""Rolling recording system — runs in the dedicated `recorder` compose
service (celery worker -Q recorder), so it survives worker/inference
rebuilds. ffmpeg subprocesses are children of THIS container.

Windows (EAT), 24/7 for key/entrance cameras:
    00:00-07:00  window "<date>_0000"  (25200s)
    07:00-14:00  window "<date>_0700"  (25200s)
    14:00-19:00  window "<date>_1400"  (18000s)
    19:00-20:00  window "<date>_1900"  ( 3600s)
    20:00-24:00  window "<date>_2000"  (14400s)
Overnight coverage is intentional: intrusion evidence is most valuable
outside trading hours. Only key cameras are recorded. Completed source
windows are retained for a bounded recovery period, allowing delayed incident
clip extraction without turning the recorder into an unbounded archive.

At each transition the PREVIOUS window is finalised. A separate hourly task
deletes source windows after the configured retention period. Recording is
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

import hashlib
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
from sqlalchemy import or_

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Ordered windows: (start_hour, end_hour, suffix, seconds).
_WINDOWS = [
    (0, 7, "0000", 25200),
    (7, 14, "0700", 25200),
    (14, 19, "1400", 18000),
    (19, 20, "1900", 3600),
    (20, 24, "2000", 14400),
]

_PID_KEY_FMT = "vg:recording:pid:{cam}"          # → json {pid, window_id, path}
_CURRENT_WINDOW_KEY = "vg:recording:current_window"


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
    None only if the window configuration has a gap."""
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


def _entrance_clip_for(db, ev, ev_ts):
    """Entrance-camera preference for store-open clips (Issue 3, Aug
    2026): shop_opened_via_occupancy / inferred alerts anchor on
    counter/aisle cameras, so their clip showed the inside of the store
    instead of the door opening. Returns an ACTIVE RecordingClip from
    one of the store's entry_exit-tagged cameras covering the event
    time, or None (caller keeps the event camera's clip). No-op when
    the event camera already IS an entrance camera."""
    from app.models import Camera, RecordingClip, Zone
    store_id = (ev.extra or {}).get("store_id")
    if store_id is None and ev.camera_id:
        cam = db.get(Camera, ev.camera_id)
        store_id = cam.store_id if cam else None
    if store_id is None:
        return None
    cam_ids = [c for (c,) in db.query(Camera.id)
                 .filter(Camera.store_id == int(store_id)).all()]
    if not cam_ids:
        return None
    zs = (db.query(Zone.camera_id, Zone.detection_types_json)
            .filter(Zone.camera_id.in_(cam_ids)).all())
    entrance = {cid for cid, types in zs if "entry_exit" in (types or [])}
    if not entrance or ev.camera_id in entrance:
        return None
    # No status filter: window files are deleted at rollover anyway, so
    # the Path.exists() check at every call site is the real gate — a
    # status filter only created a boundary race for alerts processed
    # right after a window transition.
    candidates = (db.query(RecordingClip)
                    .filter(RecordingClip.camera_id.in_(sorted(entrance)),
                            RecordingClip.started_at <= ev_ts)
                    .order_by(RecordingClip.started_at.desc())
                    .limit(20)
                    .all())
    return next((clip for clip in candidates
                 if _recording_covers_event(clip, ev_ts)), None)


def _as_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def _recording_covers_event(clip, event_ts: datetime) -> bool:
    """Prove that a recording row/file covers the event instant.

    A `started_at <= event` test alone is unsafe: an old row can survive a
    recorder crash with `ended_at=NULL`, and a prior window's file may still
    exist briefly.  For closed rows the durable end timestamp is authoritative.
    For open rows, require the file to have been written at approximately the
    event time as an additional stale-recorder guard.
    """
    if not clip or not clip.started_at or not clip.file_path:
        return False
    event_utc = _as_utc(event_ts)
    if _as_utc(clip.started_at) > event_utc:
        return False
    if clip.ended_at is not None:
        return _as_utc(clip.ended_at) >= event_utc
    target = Path(clip.file_path)
    if not target.is_file():
        return False
    try:
        # Fragmented MP4 writes may update in chunks. Two minutes tolerates a
        # slow share while rejecting a recorder that stopped before the event.
        return target.stat().st_mtime >= event_utc.timestamp() - 120
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clip_source_matches_event(db, ev, clip) -> bool:
    """Allow alternate-camera evidence only for a same-store entrance.

    Store-open alerts may intentionally be anchored on an occupancy camera
    while showing the door camera. Every other cross-camera attachment is an
    evidence mismatch and must be rejected.
    """
    if int(clip.camera_id) == int(ev.camera_id):
        return True
    if (ev.detection_type or "") != "shop_open_close":
        return False
    from app.models import Camera, Zone
    event_store_id = (ev.extra or {}).get("store_id")
    if event_store_id is None:
        event_store_id = db.query(Camera.store_id).filter(
            Camera.id == ev.camera_id).scalar()
    source_store_id = clip.store_id
    if source_store_id is None:
        source_store_id = db.query(Camera.store_id).filter(
            Camera.id == clip.camera_id).scalar()
    if (event_store_id is None or source_store_id is None
            or int(event_store_id) != int(source_store_id)):
        return False
    source_zones = (db.query(Zone.detection_types_json)
                      .filter(Zone.camera_id == clip.camera_id).all())
    return any("entry_exit" in (types or []) for (types,) in source_zones)


def _pid_is_ffmpeg(pid: int) -> bool:
    """True if `pid` is alive AND is one of our ffmpeg recorders. Guards
    against acting on a PID that a stale Redis key names but which, after a
    container restart, now belongs to an unrelated (reused) process."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"ffmpeg" in fh.read()
    except Exception:
        return False


def _any_recording_alive(r) -> bool:
    """True if at least one tracked ffmpeg recorder is still running."""
    for key in r.scan_iter(match="vg:recording:pid:*", count=200):
        try:
            pid = int(json.loads(r.get(key) or "{}").get("pid") or 0)
        except Exception:
            pid = 0
        if pid and _pid_is_ffmpeg(pid):
            return True
    return False


def _stop_all(r) -> None:
    """SIGTERM every tracked ffmpeg, then SIGKILL survivors after a grace.
    Only signals PIDs verified to be ffmpeg (a stale key after a restart
    could otherwise name a reused, unrelated PID)."""
    pids: list[int] = []
    for key in r.scan_iter(match="vg:recording:pid:*", count=200):
        try:
            pid = int(json.loads(r.get(key) or "{}").get("pid") or 0)
        except Exception:
            pid = 0
        if pid and _pid_is_ffmpeg(pid):
            pids.append(pid)
        r.delete(key)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except Exception: pass
    if pids:
        time.sleep(3)   # brief grace; most self-terminated at -t already
    for pid in pids:
        try: os.kill(pid, signal.SIGKILL)
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


def _close_window(db, window_id: str, *, ended_at: datetime | None = None) -> int:
    """Finalise a source window without destroying recoverable evidence."""
    from app.models import RecordingClip
    ended_at = ended_at or datetime.now(timezone.utc)
    updated = (db.query(RecordingClip)
                 .filter(RecordingClip.window_id == window_id,
                         RecordingClip.status == "recording")
                 .update({"status": "completed", "ended_at": ended_at},
                         synchronize_session=False))
    db.commit()
    log.info("Completed recording window %s (%d camera files retained)",
             window_id, updated)
    return int(updated)


def _prune_expired_source_windows(
    db, *, now: datetime | None = None, retention_hours: int | None = None,
) -> int:
    """Delete only fully completed source windows beyond retention.

    The per-window safety check prevents a stale completed row from deleting a
    directory that is still shared by an active recorder after a restart.
    """
    from datetime import timedelta
    from app.models import RecordingClip
    now = now or datetime.now(timezone.utc)
    hours = (int(retention_hours) if retention_hours is not None else
             int(getattr(settings, "recording_source_retention_hours", 8)))
    cutoff = now - timedelta(hours=max(1, hours))
    candidates = [wid for (wid,) in (
        db.query(RecordingClip.window_id)
          .filter(RecordingClip.status == "completed",
                  RecordingClip.ended_at.is_not(None),
                  RecordingClip.ended_at < cutoff)
          .distinct()
          .all()
    )]
    pruned = 0
    for window_id in candidates:
        unsafe = (db.query(RecordingClip.id)
                    .filter(RecordingClip.window_id == window_id,
                            or_(RecordingClip.status != "completed",
                                RecordingClip.ended_at.is_(None),
                                RecordingClip.ended_at >= cutoff))
                    .first())
        if unsafe:
            continue
        _delete_window(db, window_id)
        pruned += 1
    return pruned


@celery_app.task(name="recorder.tick", ignore_result=True)
def tick() -> None:
    """Every 60s: drive window start/stop/finalise off the EAT wall clock."""
    if not bool(getattr(settings, "recording_enabled", True)):
        return
    from app.database import SessionLocal
    r = _redis()
    now_eat = _eat_now()
    win = _current_window(now_eat)
    prev = r.get(_CURRENT_WINDOW_KEY)

    # A configuration gap → ensure everything is stopped + finalised.
    if win is None:
        if prev:
            _stop_all(r)
            with SessionLocal() as db:
                _close_window(db, prev)
            r.delete(_CURRENT_WINDOW_KEY)
        return

    window_id, seconds, wstart = win
    if prev == window_id:
        # Already the current window. Normally nothing to do — but if the
        # recorder container restarted mid-window, the Redis window marker
        # persists while all ffmpeg children died with the old container.
        # Detect that (no live ffmpeg) and respawn for the REMAINING window
        # time, instead of silently recording nothing until 14:00/19:00.
        if not _any_recording_alive(r):
            remaining = int((wstart.timestamp() + seconds) - now_eat.timestamp())
            if remaining > 30:
                _stop_all(r)                     # clear stale pid keys
                with SessionLocal() as db:
                    _start_window(db, r, window_id, remaining)
                log.warning("recorder: recovered mid-window %s after restart "
                            "(%ds remaining)", window_id, remaining)
        return
    # Transition: stop + retain the previous source window, start the new one.
    _stop_all(r)
    with SessionLocal() as db:
        if prev:
            _close_window(db, prev)
        _start_window(db, r, window_id, seconds)
    r.set(_CURRENT_WINDOW_KEY, window_id, ex=6 * 3600)


# ── Alert clip extraction ─────────────────────────────────────────────────


def _extract_one(db, alert, ev, clip, ev_ts) -> bool:
    """Cut + re-encode ONE alert clip from `clip` and stamp
    extra.alert_clip_path. Shared by the fast-path join pass and the
    24h shop_open_close backfill pass. Returns True on success.

    Standardised 30s clip (10s before + 20s after) for most types;
    shop_open_close gets the 60s "Yaya 9:13" clip — 45s of approach +
    15s of the door opening. RE-ENCODED (libx264+aac+faststart), not
    stream-copied: the window recordings are fragmented mp4, which
    HTML5 <video> can't play directly."""
    if (not _recording_covers_event(clip, ev_ts)
            or not _clip_source_matches_event(db, ev, clip)):
        log.warning(
            "recorder: rejected unbound evidence alert=%s cam=%s src_cam=%s",
            alert.id, ev.camera_id, getattr(clip, "camera_id", None),
        )
        return False
    started = clip.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (ev.detection_type or "") == "shop_open_close":
        pre_buffer, post_buffer = 45, 15      # approach + door opening
    else:
        pre_buffer, post_buffer = 10, 20
    dur = pre_buffer + post_buffer
    offset = max(0, int((ev_ts - started).total_seconds()) - pre_buffer)
    out = _alert_clips_root() / f"{alert.id}.mp4"
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
           "-ss", str(offset), "-i", clip.file_path,
           "-t", str(dur),
           "-c:v", "libx264", "-preset", "veryfast",
           "-c:a", "aac",
           "-movflags", "+faststart", str(out)]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=180)
    except Exception as e:
        log.warning("recorder: clip extract failed alert=%s: %s", alert.id, e)
        return False
    if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
        ev.extra = {
            **(ev.extra or {}),
            "alert_clip_path": str(out),
            "alert_clip_source_camera_id": int(clip.camera_id),
            "alert_clip_source_recording_id": int(clip.id),
            "alert_clip_event_timestamp": _as_utc(ev_ts).isoformat(),
            "alert_clip_offset_seconds": offset,
            "alert_clip_sha256": _sha256_file(out),
        }
        if bool(getattr(settings, "incident_foundations_enabled", False)):
            try:
                from app.services.incident_foundations import (
                    sync_evidence_manifest,
                )
                sync_evidence_manifest(db, alert, ev)
            except Exception as e:
                log.warning(
                    "recorder: evidence-manifest sync failed alert=%s: %s",
                    alert.id, e,
                )
        db.commit()
        log.info("recorder: extracted clip alert=%s cam=%s src_cam=%s dur=%ds",
                 alert.id, ev.camera_id, clip.camera_id, dur)
        return True
    return False


@celery_app.task(name="recorder.extract_pending_clips", ignore_result=True)
def extract_pending_clips() -> None:
    """Every 60s: recover alert clips while their source recording exists.

    The recording_clips table is the single source of truth — there is NO
    clip_pending flag and no dependency on the Redis window marker: we join
    alerts to active or retained completed recordings that cover the alert.
    The lookback matches source retention, so a brief extractor outage does
    not permanently discard incident evidence. Works for ALL detection types.
    Idempotent via the alert_clip_path guard."""
    if not bool(getattr(settings, "recording_enabled", True)):
        return
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent, RecordingClip
    recovery_hours = int(getattr(settings, "recording_source_retention_hours", 8))
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, recovery_hours))
    _alert_clips_root().mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        rows = (db.query(Alert, DetectionEvent, RecordingClip)
                  .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                  .join(RecordingClip,
                        RecordingClip.camera_id == DetectionEvent.camera_id)
                  .filter(Alert.created_at >= since,
                          RecordingClip.status.in_(("recording", "completed")),
                          DetectionEvent.timestamp >= RecordingClip.started_at,
                          or_(RecordingClip.ended_at.is_(None),
                              RecordingClip.ended_at >= DetectionEvent.timestamp))
                  .order_by(RecordingClip.started_at.desc())
                  .limit(1000)
                  .all())
        seen: set[int] = set()
        for alert, ev, clip in rows:
            if alert.id in seen:          # a camera can have >1 window row
                continue
            if (ev.extra or {}).get("alert_clip_path"):
                seen.add(alert.id)
                continue
            ev_ts = ev.timestamp
            if ev_ts.tzinfo is None:
                ev_ts = ev_ts.replace(tzinfo=timezone.utc)
            if not _recording_covers_event(clip, ev_ts):
                continue
            seen.add(alert.id)
            # Store-open clips must show the DOOR: prefer an entrance
            # camera's recording over the (possibly occupancy) camera
            # that anchored the event.
            if (ev.detection_type or "") == "shop_open_close":
                alt = _entrance_clip_for(db, ev, ev_ts)
                if (alt is not None and alt.file_path
                        and Path(alt.file_path).exists()):
                    clip = alt
            _extract_one(db, alert, ev, clip, ev_ts)

        # ── PASS 2: shop_open_close backfill (last 24h) ─────────────────
        # THE FIX (Aug 2026) for beat-created store-open alerts with no
        # clips: the inner JOIN above requires the alert's OWN camera to
        # have a recording row, so alerts anchored on non-recorded
        # cameras (occupancy anchors, ffmpeg spawn failures) never
        # reached the loop — and the entrance-preference swap inside it
        # never ran for exactly the alerts that needed it. The 10-min
        # `since` also aged out anything that missed one cycle. This
        # pass selects the ALERTS first (24h window, no clip yet, no
        # camera-join precondition), then resolves the best clip per
        # alert: entrance camera first, the event's own camera second.
        # Bounded (limit 200) + idempotent via the alert_clip_path guard.
        day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
        pending = (db.query(Alert, DetectionEvent)
                     .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                     .filter(Alert.created_at >= day_ago,
                             DetectionEvent.detection_type == "shop_open_close")
                     .order_by(Alert.created_at.desc())
                     .limit(200)
                     .all())
        for alert, ev in pending:
            if alert.id in seen or (ev.extra or {}).get("alert_clip_path"):
                continue
            seen.add(alert.id)
            ev_ts = ev.timestamp
            if ev_ts is None:
                continue
            if ev_ts.tzinfo is None:
                ev_ts = ev_ts.replace(tzinfo=timezone.utc)
            clip = _entrance_clip_for(db, ev, ev_ts)
            if clip is None and ev.camera_id:
                candidates = (db.query(RecordingClip)
                                .filter(RecordingClip.camera_id == ev.camera_id,
                                        RecordingClip.started_at <= ev_ts)
                                .order_by(RecordingClip.started_at.desc())
                                .limit(20)
                                .all())
                clip = next((row for row in candidates
                             if _recording_covers_event(row, ev_ts)), None)
            if (clip is None or not clip.file_path
                    or not _recording_covers_event(clip, ev_ts)):
                continue
            _extract_one(db, alert, ev, clip, ev_ts)


@celery_app.task(name="recorder.prune_source_recordings", ignore_result=True)
def prune_source_recordings() -> None:
    """Hourly: remove expired source windows after recovery has had time."""
    if not bool(getattr(settings, "recording_enabled", True)):
        return
    from app.database import SessionLocal
    with SessionLocal() as db:
        pruned = _prune_expired_source_windows(db)
    if pruned:
        log.info("recorder: pruned %d expired source windows", pruned)


@celery_app.task(name="recorder.prune_alert_clips", ignore_result=True)
def prune_alert_clips() -> None:
    """Delete alert clips older than the retention window (default 48h) and
    clear their extra path."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent
    from app.services.incident_foundations import sync_evidence_manifest
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
            provenance_keys = {
                "alert_clip_path", "alert_clip_source_camera_id",
                "alert_clip_source_recording_id", "alert_clip_event_timestamp",
                "alert_clip_offset_seconds", "alert_clip_sha256",
            }
            ev.extra = {k: v for k, v in (ev.extra or {}).items()
                        if k not in provenance_keys}
            sync_evidence_manifest(db, _alert, ev)
            cleared += 1
        if cleared:
            db.commit()
            log.info("recorder: pruned %d alert clips", cleared)


@celery_app.task(name="recorder.backfill_evidence_hashes", ignore_result=True)
def backfill_evidence_hashes() -> None:
    """Bounded legacy evidence verification; never touches inference."""
    from app.database import SessionLocal
    from app.services.incident_foundations import (
        backfill_evidence_hashes as backfill_batch,
    )
    with SessionLocal() as db:
        result = backfill_batch(db, limit=100)
        db.commit()
    if result["processed"]:
        log.info("recorder: legacy evidence verification %s", result)


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
