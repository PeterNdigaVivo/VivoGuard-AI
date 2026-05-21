"""/alerts endpoints — list with filters, server-rendered titles, snapshot.

May-2026 redesign: every alert returned by /alerts includes
`title`, `body`, `severity`, and `snapshot_url` pre-computed by the
server. The frontend stops translating detection_type strings (which
made the chain /alerts page and the per-store feed render
inconsistently) and just renders what we send.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import Alert, Camera, DetectionEvent, User, Zone
from app.schemas.alert import AlertActionOut, AlertNoteIn, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


# ---- Title / body / severity translation ---------------------------
#
# Single source of truth for the human-readable alert presentation.
# Same logic feeds the per-store dashboard feed AND the chain /alerts
# page so labels are consistent.

# Severity bucket per detection_type. Front-end pulls from this for
# the colour (red / amber / blue dots).
_SEVERITY: dict[str, str] = {
    # critical — security / safety incidents
    "fight":             "critical",
    "intrusion":         "critical",
    "weapon":            "critical",
    "weapon_brandished": "critical",
    "fall":              "critical",
    "fire":              "critical",
    "smoke":             "critical",
    "shrinkage":         "critical",
    # warning — operational risks
    "queue":             "warning",
    "queue_length":      "warning",
    "crowd":             "warning",
    "trespass":          "warning",
    "loitering":         "warning",
    "shutter":           "warning",
    "abandoned_object":  "warning",
    "tailgating":        "warning",
    # info — routine operational signals
    "staff_present":     "info",
    "occupancy":         "info",
    "entry_exit":        "info",
    "dwell":             "info",
    "passersby":         "info",
}


def _severity(detection_type: str | None) -> str:
    return _SEVERITY.get(detection_type or "", "info")


# Per-type emoji prefix for the title — the spec called these out
# explicitly. Operators scan the feed quickly; the icon does the
# heavy lifting for category recognition.
_TITLE_ICONS: dict[str, str] = {
    "fight":             "🚨",
    "intrusion":         "🚨",
    "trespass":          "🚨",
    "weapon":            "🚨",
    "weapon_brandished": "🚨",
    "fall":              "🚨",
    "fire":              "🔥",
    "smoke":             "💨",
    "shrinkage":         "⚠️",
    "queue":             "⏱️",
    "queue_length":      "⏱️",
    "crowd":             "⚠️",
    "loitering":         "⚠️",
    "shutter":           "🔒",
    "abandoned_object":  "🧳",
    "tailgating":        "⚠️",
    "staff_present":     "👤",
    "occupancy":         "📊",
}


def _icon(detection_type: str | None) -> str:
    return _TITLE_ICONS.get(detection_type or "", "•")


def _extract(extra: dict | None, *keys, default=None):
    """Best-effort getter across an alert's `event.extra` blob — the
    detector usually drops the contextual numbers in there
    (`duration_min`, `count`, `wait_seconds`, etc)."""
    if not isinstance(extra, dict):
        return default
    for k in keys:
        if k in extra and extra[k] is not None:
            return extra[k]
    return default


def _title(event: DetectionEvent, camera: Camera | None) -> str:
    """Human-readable title for an alert. Embeds the camera name so
    operators triaging a long feed know WHICH camera fired."""
    cam = camera.name if camera else "unknown camera"
    dt = event.detection_type or "alert"
    extra = event.extra or {}
    icon = _icon(dt)

    if dt == "staff_present":
        mins = _extract(extra, "unstaffed_minutes", "duration_min", default=None)
        if mins is not None:
            return f"{icon} Counter Unstaffed for {int(round(float(mins)))} min — {cam}"
        return f"{icon} Counter Unstaffed — {cam}"
    if dt in ("queue", "queue_length"):
        n = _extract(extra, "queue_length", "count", default=None)
        if n is not None:
            return f"{icon} Long Queue — {int(n)} people waiting — {cam}"
        return f"{icon} Long Queue — {cam}"
    if dt == "intrusion":
        return f"{icon} After-hours Intrusion Detected — {cam}"
    if dt == "crowd":
        n = _extract(extra, "count", "people", default=None)
        if n is not None:
            return f"{icon} Crowd Alert — {int(n)} people in zone — {cam}"
        return f"{icon} Crowd Alert — {cam}"
    if dt == "shutter":
        state = _extract(extra, "state", "shutter_state", default="")
        return f"{icon} Shutter {state or 'state change'} — {cam}"
    if dt == "trespass":
        return f"{icon} Unauthorised person in restricted zone — {cam}"
    if dt == "fight":
        return f"{icon} Incident / fight detected — {cam}"
    if dt == "occupancy":
        pct = _extract(extra, "capacity_pct", default=None)
        if pct is not None:
            return f"{icon} Store at {int(round(float(pct)))}% capacity — {cam}"
        return f"{icon} Occupancy alert — {cam}"
    if dt == "shrinkage":
        return f"{icon} Potential loss-prevention alert — {cam}"
    if dt == "fall":
        return f"{icon} Person fall detected — {cam}"
    if dt == "tailgating":
        return f"{icon} Tailgate detected — {cam}"
    if dt == "abandoned_object":
        return f"{icon} Abandoned item — {cam}"
    if dt == "loitering":
        mins = _extract(extra, "duration_min", default=None)
        if mins is not None:
            return f"{icon} Loitering for {int(round(float(mins)))} min — {cam}"
        return f"{icon} Loitering — {cam}"

    # Fallback: Titlecase the snake_case detection type.
    label = dt.replace("_", " ").title()
    return f"{icon} {label} — {cam}"


def _body(event: DetectionEvent, zone: Zone | None) -> str:
    """One-sentence description with the relevant operational
    implication. These are the bits store managers actually need to
    decide whether to act."""
    dt = event.detection_type or ""
    extra = event.extra or {}
    zone_name = zone.name if zone else None

    if dt == "staff_present":
        mins = _extract(extra, "unstaffed_minutes", "duration_min", default=None)
        if mins is not None:
            return (f"Staff has not been detected at the service counter for "
                    f"the past {int(round(float(mins)))} minutes. Customer service "
                    f"may be affected.")
        return ("Staff has not been detected at the service counter. "
                "Customer service may be affected.")
    if dt in ("queue", "queue_length"):
        n = _extract(extra, "queue_length", "count", default=None)
        wait = _extract(extra, "wait_seconds", "queue_wait_seconds", default=None)
        sentences = []
        if n is not None:
            sentences.append(f"Queue has grown to {int(n)} people.")
        if wait is not None:
            sentences.append(f"Average wait time is estimated at {int(round(float(wait) / 60))} minutes.")
        sentences.append("Consider opening an additional checkout point.")
        return " ".join(sentences)
    if dt == "intrusion":
        return ("Motion detected inside the store outside business hours. "
                "Verify whether this is authorised staff or an actual intrusion.")
    if dt == "crowd":
        n = _extract(extra, "count", "people", default=None)
        if n is not None:
            return (f"{int(n)} people detected in {zone_name or 'a single zone'} simultaneously. "
                    f"Monitor for safety and check for bottlenecks.")
        return ("Large gathering detected. Monitor for safety.")
    if dt == "shutter":
        return ("Shutter open/close state has changed. Verify against expected store hours.")
    if dt == "trespass":
        return (f"An unauthorised person was detected in {zone_name or 'a restricted area'}. "
                f"Investigate and respond per protocol.")
    if dt == "fight":
        return ("Aggressive or fighting behaviour detected. Dispatch staff to scene immediately.")
    if dt == "fall":
        return ("A person appears to have fallen. Send help and check for injury.")
    if dt == "occupancy":
        pct = _extract(extra, "capacity_pct", default=None)
        if pct is not None:
            return (f"Store occupancy is at {int(round(float(pct)))}% of stated capacity. "
                    f"Watch for crowding at peak.")
        return ("Occupancy threshold reached.")
    if dt == "shrinkage":
        return ("Behaviour consistent with potential shoplifting detected. "
                "Review footage and dispatch staff if appropriate.")
    if dt == "tailgating":
        return ("Multiple people passed through a single-entry zone together. "
                "Verify each is authorised.")
    if dt == "abandoned_object":
        return ("An object has been left unattended in the store. Investigate.")
    if dt == "loitering":
        return ("A person has been lingering in one area beyond the typical browsing window.")

    return "Review the footage and decide whether to confirm or dismiss."


def _snapshot_url(alert_id: int, event: DetectionEvent) -> str:
    """Browser-fetchable URL for an alert's snapshot. Always returns
    a URL — the endpoint itself falls back to the camera's latest
    cached frame when the event has no archived thumbnail. Frontend
    can <img src> this directly; on 404 it renders the camera-icon
    placeholder."""
    return f"/api/alerts/{alert_id}/snapshot"


def _time_range(event: DetectionEvent, store) -> str | None:
    """Human-readable when-it-happened line for the alert card.

    Duration-style events (dwell, staff_present, loitering,
    abandoned_object) show a "From 9:30 PM to 9:55 PM" range, computed
    from the event's known duration field. Point-in-time events
    (intrusion, fight, queue, crowd) just show "At 9:30 PM".

    Timestamps are formatted in the STORE's timezone so a Junction
    manager sees 9:30 PM Nairobi, a future Kampala manager sees
    9:30 PM EAT, etc.
    """
    if not event or not event.timestamp:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz_name = (getattr(store, "timezone", None) or "Africa/Nairobi") if store else "Africa/Nairobi"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Africa/Nairobi")
    except ImportError:
        tz = None     # py<3.9 fallback — extremely unlikely on our image
    ts = event.timestamp
    if tz:
        ts_local = ts.astimezone(tz)
    else:
        ts_local = ts
    fmt = "%-I:%M %p"   # "9:30 PM" — POSIX strftime; Windows would need %#I
    try:
        end_str = ts_local.strftime(fmt)
    except Exception:
        # Windows compatibility (the Docker host is usually Linux but
        # guard anyway).
        end_str = ts_local.strftime("%I:%M %p").lstrip("0")

    # Extract the per-event duration when the detector provided it.
    extra = event.extra or {}
    duration_seconds: float | None = None
    dt = event.detection_type or ""
    if dt == "staff_present":
        m = (extra.get("unstaffed_minutes") or extra.get("duration_min")
             or extra.get("duration_minutes"))
        if m is not None:
            try:
                duration_seconds = float(m) * 60.0
            except Exception:
                duration_seconds = None
    elif dt == "dwell":
        s = (extra.get("dwell_seconds") or extra.get("duration_seconds")
             or extra.get("duration_sec"))
        if s is not None:
            try:
                duration_seconds = float(s)
            except Exception:
                duration_seconds = None
    elif dt == "loitering":
        m = extra.get("duration_min") or extra.get("duration_minutes")
        if m is not None:
            try:
                duration_seconds = float(m) * 60.0
            except Exception:
                duration_seconds = None
    elif dt == "abandoned_object":
        s = extra.get("duration_seconds") or extra.get("dwell_seconds")
        if s is not None:
            try:
                duration_seconds = float(s)
            except Exception:
                duration_seconds = None

    if duration_seconds and duration_seconds >= 60:
        from datetime import timedelta
        start_ts = ts_local - timedelta(seconds=duration_seconds)
        try:
            start_str = start_ts.strftime(fmt)
        except Exception:
            start_str = start_ts.strftime("%I:%M %p").lstrip("0")
        mins = int(round(duration_seconds / 60))
        return f"🕒 Between {start_str} and {end_str} ({mins} min)"
    # Point-in-time event.
    return f"🕒 Detected at {end_str}"


def _to_alert_out(alert: Alert, event: DetectionEvent,
                  camera: Camera | None, zone: Zone | None,
                  store=None) -> AlertOut:
    item = AlertOut.model_validate(alert)
    item.camera_id      = event.camera_id
    item.camera_name    = camera.name if camera else None
    item.detection_type = event.detection_type
    item.confidence     = event.confidence
    item.bbox_norm      = event.bbox_json
    item.zone_id        = event.zone_id
    item.zone_name      = zone.name if zone else None
    item.thumbnail_path = event.thumbnail_path
    item.severity       = _severity(event.detection_type)
    item.title          = _title(event, camera)
    item.body           = _body(event, zone)
    item.time_range     = _time_range(event, store)
    item.snapshot_url   = _snapshot_url(alert.id, event)
    return item


# ---- Endpoints -----------------------------------------------------

@router.get("", response_model=list[AlertOut])
def list_alerts(
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
    camera_id: Optional[int]   = Query(None),
    store_id: Optional[int]    = Query(None),
    detection_type: Optional[str] = Query(None),
    zone_id: Optional[int]     = Query(None),
    status: Optional[str]      = Query(None),
    since: Optional[datetime]  = Query(None),
    until: Optional[datetime]  = Query(None),
    limit: int                 = Query(100, le=500),
):
    q = (db.query(Alert, DetectionEvent, Camera)
           .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
           .outerjoin(Camera, DetectionEvent.camera_id == Camera.id))
    if status:
        q = q.filter(Alert.status == status)
    if camera_id:
        q = q.filter(DetectionEvent.camera_id == camera_id)
    if store_id is not None:
        q = q.filter(Camera.store_id == store_id)
    if detection_type:
        q = q.filter(DetectionEvent.detection_type == detection_type)
    if zone_id:
        q = q.filter(DetectionEvent.zone_id == zone_id)
    if since:
        q = q.filter(DetectionEvent.timestamp >= since)
    if until:
        q = q.filter(DetectionEvent.timestamp <= until)
    q = q.order_by(desc(Alert.created_at)).limit(limit)

    rows = q.all()
    # Bulk-fetch zones AND stores referenced by these events so
    # _to_alert_out gets the names without N round-trips. Skipped
    # when no event in the page references one.
    zone_ids  = {ev.zone_id for _, ev, _ in rows if ev.zone_id is not None}
    store_ids = {cam.store_id for _, _, cam in rows if cam and cam.store_id is not None}
    zones_by_id  = ({z.id: z for z in db.query(Zone).filter(Zone.id.in_(zone_ids)).all()}
                    if zone_ids else {})
    from app.models import Store as _Store
    stores_by_id = ({s.id: s for s in db.query(_Store).filter(_Store.id.in_(store_ids)).all()}
                    if store_ids else {})

    return [
        _to_alert_out(
            a, ev, cam,
            zones_by_id.get(ev.zone_id) if ev.zone_id else None,
            stores_by_id.get(cam.store_id) if (cam and cam.store_id) else None,
        )
        for a, ev, cam in rows
    ]


@router.post("/{alert_id}/confirm", response_model=AlertActionOut)
def confirm(alert_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_role("admin", "operator"))):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = "confirmed"
    a.assigned_to = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    try:
        from app.training.feedback_loop import absorb_confirmed
        absorb_confirmed(db, a.id)
    except Exception:
        pass
    db.commit()
    return AlertActionOut(id=a.id, status=a.status)


@router.post("/{alert_id}/dismiss", response_model=AlertActionOut)
def dismiss(alert_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_role("admin", "operator"))):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = "dismissed"
    a.assigned_to = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    try:
        from app.training.feedback_loop import mark_dismissed
        mark_dismissed(db, a.id)
    except Exception:
        pass
    db.commit()
    return AlertActionOut(id=a.id, status=a.status)


@router.post("/{alert_id}/resolve", response_model=AlertActionOut)
def resolve(alert_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_role("admin", "operator"))):
    """Mark an alert as resolved. Distinct from /confirm (which feeds
    training as a true positive); /resolve just records that the
    operator dealt with whatever was happening on the ground. Use
    this for routine operational alerts (queue, staff_present, …) so
    confirm/dismiss can stay narrowly about ML feedback quality."""
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = "confirmed"     # reuse the bucket — frontend treats as resolved
    a.assigned_to = user.id
    a.acknowledged_at = datetime.now(timezone.utc)
    a.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return AlertActionOut(id=a.id, status=a.status)


@router.post("/{alert_id}/note", response_model=AlertOut)
def add_note(alert_id: int, body: AlertNoteIn,
             db: Session = Depends(get_db),
             user: User = Depends(require_role("admin", "operator"))):
    """Append an investigation note. We APPEND rather than overwrite
    so multiple operators can leave a trail. Each entry is stamped
    with the username + timestamp so the trail reads chronologically."""
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{when}] {user.email}: {body.note.strip()}"
    a.notes = f"{a.notes}\n{line}" if a.notes else line
    db.commit()
    db.refresh(a)
    # Re-fetch with joins so the response shape matches /alerts.
    from app.models import Store as _Store
    ev = db.get(DetectionEvent, a.event_id)
    cam = db.get(Camera, ev.camera_id) if ev and ev.camera_id else None
    zone = db.get(Zone, ev.zone_id) if ev and ev.zone_id else None
    store = db.get(_Store, cam.store_id) if (cam and cam.store_id) else None
    return _to_alert_out(a, ev, cam, zone, store)


@router.get("/{alert_id}/snapshot")
def alert_snapshot(alert_id: int, db: Session = Depends(get_db),
                   _u: User = Depends(get_current_user)):
    """Return a JPEG snapshot for the alert.

    Strategy:
      1. If the event has a stored thumbnail_path on disk, serve that
         (the moment-of-detection frame).
      2. Otherwise fall back to the camera's most recent cached frame
         from the streamer (vg:frame:{camera_id} in Redis). Stale by
         a few seconds but always available for active cameras.
      3. Otherwise 404 — frontend renders the placeholder.
    """
    from fastapi.responses import Response, FileResponse
    from app.stream.frame_buffer import FrameBuffer
    import os

    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    ev = db.get(DetectionEvent, a.event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    # Tier 1: archived thumbnail (if the inference worker stored one).
    if ev.thumbnail_path and os.path.exists(ev.thumbnail_path):
        return FileResponse(ev.thumbnail_path, media_type="image/jpeg")

    # Tier 2: camera's latest cached frame from the streamer.
    if ev.camera_id:
        cached = FrameBuffer().latest_jpeg(ev.camera_id)
        if cached:
            return Response(content=cached, media_type="image/jpeg")

    raise HTTPException(404, "no snapshot available")
