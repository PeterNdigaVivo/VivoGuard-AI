"""Fitting-room occupancy: overstay and congestion checks.

Replays the crossings EntryExitDetector already persists on changing-room
lines (zones tagged both `entry_exit` and `changing_room`) to work out who
is still inside. There is no per-frame state: the database is the source
of truth, so nothing is lost when an inference slice ends and another
process picks the camera up.

Direction follows the convention odoo_assurance already relies on: on a
changing-room line `in` is into the fitting rooms and `out` is back onto
the floor. If a camera's alerts read inverted, flip its `inward_sign`.

Cameras watch the corridor, never a cubicle, so they cannot tell which
cubicle someone used. Alerts are per corridor and worded as a service
prompt, not an accusation.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

_IN = {"in", "entry", "inward"}
_OUT = {"out", "exit", "outward"}
_TAGS = {"entry_exit", "changing_room"}


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def replay_occupancy(crossings, now: datetime, max_stay_seconds: int):
    """Return (occupancy, oldest_open_entry) from (timestamp, direction) pairs.

    Only crossings inside the last `max_stay_seconds` count. That window is
    the decay: an entry whose exit was missed ages out rather than holding
    occupancy up forever.

    An exit closes the OLDEST open entry. The camera cannot tell who left,
    and FIFO errs towards under-reporting a long stay, never inventing one.
    """
    horizon = now - timedelta(seconds=max_stay_seconds)
    open_entries: deque[datetime] = deque()
    for ts, direction in sorted(crossings, key=lambda c: c[0]):
        if ts < horizon:
            continue
        if direction in _IN:
            open_entries.append(ts)
        elif direction in _OUT and open_entries:
            open_entries.popleft()
    return len(open_entries), (open_entries[0] if open_entries else None)


@celery_app.task(name="fitting_room.check", ignore_result=True)
def fitting_room_check() -> None:
    """Per changing-room line: prompt a service check on a long stay, and
    send help when the rooms are busy. Trading hours only."""
    if not settings.fitting_room_alerts_enabled:
        return
    from app.database import SessionLocal
    from app.models import Camera, DetectionEvent, Store, Zone
    from app.tasks.alerting import _redis, _within_operating_hours

    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=settings.fitting_room_max_stay_seconds)
    r = _redis()
    with SessionLocal() as db:
        # isnot(True), not is_(False): `suppressed` is nullable, and a NULL
        # there means "not suppressed" everywhere else in the codebase.
        zones = [z for z in db.query(Zone).filter(Zone.suppressed.isnot(True)).all()
                 if _TAGS <= set(z.detection_types_json or [])]
        for zone in zones:
            try:
                camera = db.get(Camera, zone.camera_id)
                store = db.get(Store, camera.store_id) if camera and camera.store_id else None
                if camera is None or not _within_operating_hours(store):
                    continue
                rows = (db.query(DetectionEvent.timestamp, DetectionEvent.extra)
                          .filter(DetectionEvent.zone_id == zone.id,
                                  DetectionEvent.detection_type == "entry_exit",
                                  DetectionEvent.timestamp >= since)
                          .all())
                crossings = [(_aware(ts), str((extra or {}).get("direction") or ""))
                             for ts, extra in rows]
                occupancy, oldest = replay_occupancy(
                    crossings, now, settings.fitting_room_max_stay_seconds)

                if oldest is not None:
                    stay = int((now - oldest).total_seconds())
                    if stay >= settings.fitting_room_overstay_seconds:
                        # Keyed on the entry itself, so one long stay alerts once.
                        _raise(db, r, zone, camera, "fitting_room_overstay",
                               dedup=f"{zone.id}:{int(oldest.timestamp())}",
                               ttl=settings.fitting_room_max_stay_seconds,
                               stay_minutes=stay // 60, occupancy=occupancy)
                if occupancy >= settings.fitting_room_congestion_occupancy:
                    _raise(db, r, zone, camera, "fitting_room_congestion",
                           dedup=str(zone.id), ttl=settings.fitting_room_dedup_seconds,
                           occupancy=occupancy)
            except Exception:
                db.rollback()
                log.exception("fitting_room_check: zone=%s failed", zone.id)


def _raise(db, r, zone, camera, rule: str, *, dedup: str, ttl: int, **extra) -> None:
    if not r.set(f"vg:fitting_room:{rule}:{dedup}", "1", nx=True, ex=ttl):
        return
    from app.tasks.alerting import _create_info_alert
    _create_info_alert(
        db, camera_id=camera.id, zone_id=zone.id, store_id=camera.store_id,
        detection_type="fitting_room", cls=rule,
        extra={"rule": rule, "priority": "high", "store_id": camera.store_id,
               "zone_name": zone.name, **extra},
    )
    db.commit()
    log.info("fitting_room: %s zone=%s %s", rule, zone.id, extra)
