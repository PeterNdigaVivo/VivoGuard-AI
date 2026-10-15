"""Build the evaluation test set on the server (never in git).

Three sources, all gated by settings.eval_capture_raw_frames:
  • reviewed alerts — the operator's True/False verdict plus a CLEAN frame
    (the `_raw.jpg` saved beside the snapshot, or a frame cut from the
    48-hour alert clip for alerts reviewed before capture was switched on);
  • people frames — busy-hour frames from cameras that currently see
    people, for admins to click each visible person;
  • clips — the alert clip of person/counter alerts, for admins to count
    people (tracker ID-switch testing).

Files are private to root (dirs 0700, files 0600) under settings.eval_dir
and expire after settings.eval_retention_days (prune_samples).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from app.eval.categories import (
    CATEGORIES, COUNTER_UNATTENDED, EVALUATED_TYPES, PERSON, eval_category,
)
from app.models import Alert, AlertReviewDecision, Camera, DetectionEvent, EvalSample, Zone

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")
BUSY_HOURS = range(10, 20)          # 10:00–19:59 Nairobi time
PEOPLE_PER_RUN = 3
PEOPLE_CAMERA_COOLDOWN = timedelta(hours=6)
ACTIVITY_FRESH_SECONDS = 300
CLIP_EVENT_SECOND = 10              # recorder.py cuts alert clips 10 s before the event
LOOKBACK = timedelta(days=14)       # reviewed alerts older than this are not scanned
_VERDICTS = {"confirmed": "true", "dismissed": "false"}


# ---- private files ------------------------------------------------------

def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _private_copy(src: str, dst: Path) -> None:
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)


def _private_write(dst: Path, data: bytes) -> None:
    dst.write_bytes(data)
    os.chmod(dst, 0o600)


def private_write_text(dst: Path, text: str) -> None:
    dst.write_text(text)
    os.chmod(dst, 0o600)


def frame_from_clip(clip_path: str, second: float) -> bytes | None:
    """JPEG of the frame `second` seconds into a clip (clamped to the
    clip's middle when it is shorter). None when it cannot be read."""
    try:
        import cv2
        cap = cv2.VideoCapture(clip_path)
        try:
            if not cap.isOpened():
                return None
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            if fps > 0 and frames > 0:
                second = min(second, frames / fps / 2)
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, second) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                return None
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            return buf.tobytes() if ok else None
        finally:
            cap.release()
    except Exception as exc:                                    # noqa: BLE001
        log.warning("eval: could not read a frame from clip %s: %s", clip_path, exc)
        return None


# ---- reviewed alerts ----------------------------------------------------

def _latest_verdicts(db: Session, alert_ids: list[int]) -> dict[int, str]:
    latest: dict[int, str] = {}
    for row in (db.query(AlertReviewDecision)
                  .filter(AlertReviewDecision.alert_id.in_(alert_ids))
                  .order_by(AlertReviewDecision.created_at, AlertReviewDecision.id)):
        latest[row.alert_id] = row.verdict
    return {aid: _VERDICTS[v] for aid, v in latest.items() if v in _VERDICTS}


def collect_reviewed_alerts(db: Session, *, root: Path, target_per_category: int,
                            clip_target: int, batch: int = 500,
                            now: datetime | None = None) -> dict:
    """Copy newly reviewed alerts with a clean frame into the test set."""
    ensure_private_dir(root)
    frames_dir = ensure_private_dir(root / "frames")
    clips_dir = ensure_private_dir(root / "clips")
    have = dict(db.query(EvalSample.category, func.count(EvalSample.id))
                  .filter(EvalSample.kind == "alert").group_by(EvalSample.category).all())
    clips_have = db.query(func.count(EvalSample.id)).filter(EvalSample.kind == "clip").scalar() or 0
    if all(have.get(c, 0) >= target_per_category for c in CATEGORIES):
        return {"added": 0, "reason": "all categories at target"}

    sampled = db.query(EvalSample.alert_id).filter(EvalSample.alert_id.isnot(None))
    rows = (db.query(Alert, DetectionEvent)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .filter(exists().where(AlertReviewDecision.alert_id == Alert.id),
                      DetectionEvent.detection_type.in_(EVALUATED_TYPES),
                      Alert.created_at >= (now or datetime.now(timezone.utc)) - LOOKBACK,
                      Alert.id.notin_(sampled))
              .order_by(Alert.id.desc()).limit(batch).all())
    verdicts = _latest_verdicts(db, [a.id for a, _ in rows])
    stats = {"added": 0, "clips_added": 0, "no_clean_frame": 0, "not_evaluated": 0}
    for alert, ev in rows:
        extra = ev.extra or {}
        category = eval_category(ev.detection_type, extra.get("rule"))
        verdict = verdicts.get(alert.id)
        if category is None or verdict is None:
            stats["not_evaluated"] += 1
            continue
        if have.get(category, 0) >= target_per_category:
            continue
        frame_path = frames_dir / f"alert_{alert.id}.jpg"
        raw = extra.get("raw_snapshot_path")
        clip = extra.get("alert_clip_path")
        try:
            if raw and os.path.isfile(raw):
                _private_copy(raw, frame_path)
            elif clip and os.path.isfile(clip):
                data = frame_from_clip(clip, CLIP_EVENT_SECOND)
                if data is None:
                    stats["no_clean_frame"] += 1
                    continue
                _private_write(frame_path, data)
            else:
                stats["no_clean_frame"] += 1
                continue
        except OSError as exc:
            log.warning("eval: could not store frame for alert %s: %s", alert.id, exc)
            continue
        zone = db.get(Zone, ev.zone_id) if ev.zone_id else None
        db.add(EvalSample(
            kind="alert", alert_id=alert.id, camera_id=ev.camera_id,
            category=category, detection_type=ev.detection_type, verdict=verdict,
            frame_path=str(frame_path),
            zone_polygon=zone.polygon_coords_json if zone else None,
            event_bbox=ev.bbox_json, status="labelled", captured_at=ev.timestamp))
        have[category] = have.get(category, 0) + 1
        stats["added"] += 1
        if (category in (PERSON, COUNTER_UNATTENDED) and clip and os.path.isfile(clip)
                and clips_have < clip_target):
            clip_path = clips_dir / f"alert_{alert.id}.mp4"
            try:
                _private_copy(clip, clip_path)
                db.add(EvalSample(kind="clip", alert_id=alert.id, camera_id=ev.camera_id,
                                  category=category, detection_type=ev.detection_type,
                                  clip_path=str(clip_path), status="pending",
                                  captured_at=ev.timestamp))
                clips_have += 1
                stats["clips_added"] += 1
            except OSError as exc:
                log.warning("eval: could not store clip for alert %s: %s", alert.id, exc)
    db.commit()
    return stats


# ---- busy-hour people frames ----------------------------------------------

def busy_cameras(db: Session, redis_client, now: datetime, *, limit: int) -> list[int]:
    """Cameras that see people right now, most people first, skipping
    cameras sampled in the last PEOPLE_CAMERA_COOLDOWN."""
    recent = {cid for (cid,) in db.query(EvalSample.camera_id)
              .filter(EvalSample.kind == "people",
                      EvalSample.created_at >= now - PEOPLE_CAMERA_COOLDOWN)}
    candidates: list[tuple[int, int]] = []
    for (cid,) in db.query(Camera.id):
        if cid in recent:
            continue
        raw = redis_client.get(f"vg:activity:{cid}")
        if not raw:
            continue
        try:
            act = json.loads(raw)
        except (TypeError, ValueError):
            continue
        people = int(act.get("people") or 0)
        if people > 0 and now.timestamp() - float(act.get("ts") or 0) <= ACTIVITY_FRESH_SECONDS:
            candidates.append((people, cid))
    candidates.sort(reverse=True)
    return [cid for _, cid in candidates[:limit]]


def sample_people_frames(db: Session, redis_client, frame_buffer, *, root: Path,
                         target: int, now: datetime | None = None) -> int:
    """Save up to PEOPLE_PER_RUN clean frames from busy cameras."""
    now = now or datetime.now(timezone.utc)
    if now.astimezone(EAT).hour not in BUSY_HOURS:
        return 0
    have = db.query(func.count(EvalSample.id)).filter(EvalSample.kind == "people").scalar() or 0
    if have >= target:
        return 0
    ensure_private_dir(root)
    frames_dir = ensure_private_dir(root / "frames")
    added = 0
    for cid in busy_cameras(db, redis_client, now, limit=min(PEOPLE_PER_RUN, target - have)):
        jpeg = frame_buffer.latest_jpeg(cid, prefer_overlay=False)
        if not jpeg:
            continue
        path = frames_dir / f"people_{cid}_{int(now.timestamp())}.jpg"
        try:
            _private_write(path, jpeg)
        except OSError as exc:
            log.warning("eval: could not store people frame cam=%s: %s", cid, exc)
            continue
        db.add(EvalSample(kind="people", camera_id=cid, frame_path=str(path),
                          status="pending", captured_at=now))
        added += 1
    db.commit()
    return added


# ---- retention ----------------------------------------------------------------

def prune_samples(db: Session, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete test-set samples (rows + files) older than the retention."""
    now = now or datetime.now(timezone.utc)
    old = (db.query(EvalSample)
             .filter(EvalSample.created_at < now - timedelta(days=retention_days)).all())
    for sample in old:
        for path in (sample.frame_path, sample.clip_path):
            if not path:
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("eval: could not delete %s: %s", path, exc)
        db.delete(sample)
    db.commit()
    return len(old)
