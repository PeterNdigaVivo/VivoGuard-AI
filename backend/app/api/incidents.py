"""/incidents — the operator-facing incident queue.

Every list, count, export and bulk action here goes through
app.services.incidents.incident_query with the same view and date range,
so a tab badge, the summary bar, the export and "Resolve all" can never
disagree about which incidents they mean.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import Alert, Camera, DetectionEvent, Store, User, Zone
from app.models.incident import Incident, IncidentMember, IncidentTransition
from app.schemas.incident import (
    BulkResolveIn, BulkResolveOut, IncidentAction, IncidentActionIn,
    IncidentDetail, IncidentOut, IncidentPage, IncidentSummary,
    IncidentTimelineEntry, IncidentTransitionOut, IncidentView,
)
from app.services import incidents as svc

router = APIRouter(prefix="/incidents", tags=["incidents"])

_PRIORITY_ORDER = case((Incident.priority == "urgent", 0),
                       (Incident.priority == "attention", 1), else_=2)


def _ordered(q, view: str):
    if view == "resolved":
        return q.order_by(desc(Incident.closed_at), desc(Incident.id))
    if view in ("open", "attention", "review"):
        return q.order_by(_PRIORITY_ORDER, desc(Incident.last_seen_at),
                          desc(Incident.id))
    return q.order_by(desc(Incident.last_seen_at), desc(Incident.id))


def _serialise(db: Session, incidents: list[Incident]) -> list[IncidentOut]:
    """Bulk-hydrate names and the head alert (no N+1 queries)."""
    from app.api.alerts import _to_alert_out

    if not incidents:
        return []
    cam_ids = {i.camera_id for i in incidents if i.camera_id}
    store_ids = {i.store_id for i in incidents if i.store_id}
    zone_ids = {i.zone_id for i in incidents if i.zone_id}
    head_ids = {i.head_alert_id for i in incidents if i.head_alert_id}
    cams = ({c.id: c for c in db.query(Camera).filter(Camera.id.in_(cam_ids))}
            if cam_ids else {})
    stores = ({s.id: s for s in db.query(Store).filter(Store.id.in_(store_ids))}
              if store_ids else {})
    zones = ({z.id: z for z in db.query(Zone).filter(Zone.id.in_(zone_ids))}
             if zone_ids else {})
    heads = ({a.id: (a, e) for a, e in
              db.query(Alert, DetectionEvent)
                .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                .filter(Alert.id.in_(head_ids))}
             if head_ids else {})
    out = []
    for inc in incidents:
        item = IncidentOut.model_validate(inc)
        cam = cams.get(inc.camera_id)
        store = stores.get(inc.store_id)
        zone = zones.get(inc.zone_id)
        item.camera_name = cam.name if cam else None
        item.store_name = store.name if store else None
        item.zone_name = zone.name if zone else None
        pair = heads.get(inc.head_alert_id)
        if pair is not None:
            a, ev = pair
            item.head = _to_alert_out(a, ev, cam, zone, store)
        out.append(item)
    return out


@router.get("", response_model=IncidentPage)
def list_incidents(
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
    view: IncidentView = Query("open"),
    store_id: Optional[int] = Query(None),
    camera_id: Optional[int] = Query(None),
    detection_type: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    q = svc.incident_query(db, view=view, store_id=store_id, camera_id=camera_id,
                           detection_type=detection_type, since=since, until=until)
    total = q.order_by(None).count()
    rows = _ordered(q, view).offset(offset).limit(limit).all()
    return IncidentPage(view=view, total=total, limit=limit, offset=offset,
                        items=_serialise(db, rows))


@router.get("/summary", response_model=IncidentSummary)
def incident_summary(
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
    store_id: Optional[int] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    """Summary bar AND tab counts — same query, same range as the list."""
    data = svc.summarize(db, store_id=store_id, since=since, until=until)
    return IncidentSummary(**data, lifecycle_enabled=svc.lifecycle_ready(db))


@router.post("/bulk-resolve", response_model=BulkResolveOut)
def bulk_resolve(payload: BulkResolveIn, db: Session = Depends(get_db),
                 user: User = Depends(require_role("admin", "operator"))):
    """Resolve exactly ``expected_count`` incidents, or nothing (409)."""
    try:
        ids = svc.bulk_resolve(
            db, view=payload.view, expected_count=payload.expected_count,
            actor_user_id=user.id, store_id=payload.store_id,
            since=payload.since, until=payload.until)
    except svc.IncidentActionError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    db.commit()
    return BulkResolveOut(resolved=len(ids), ids=ids)


@router.get("/export.xlsx")
def export_incidents(
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
    view: IncidentView = Query("all"),
    store_id: Optional[int] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    """The rows the operator is looking at: same view, filters and range."""
    import io
    from zoneinfo import ZoneInfo

    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    q = svc.incident_query(db, view=view, store_id=store_id, since=since, until=until)
    rows = _serialise(db, _ordered(q, view).limit(10000).all())
    eat = ZoneInfo("Africa/Nairobi")

    def fmt(dt):
        if dt is None:
            return ""
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(eat).strftime("%Y-%m-%d %H:%M")

    wb = Workbook()
    ws = wb.active
    ws.title = "Incidents"
    ws.append(["Opened (EAT)", "Last seen (EAT)", "Store", "Camera", "Incident",
               "Priority", "Status", "Events", "Closed (EAT)", "Close reason",
               "Policy reason"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1E293B")
    for r in rows:
        ws.append([fmt(r.opened_at), fmt(r.last_seen_at), r.store_name or "",
                   r.camera_name or "",
                   (r.head.plain_title if r.head and r.head.plain_title
                    else r.detection_type),
                   r.priority, r.status, r.event_count, fmt(r.closed_at),
                   r.close_reason or "", r.policy_reason or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"vivoguard_incidents_{view}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: int, db: Session = Depends(get_db),
                 _u: User = Depends(get_current_user)):
    inc = db.get(Incident, incident_id)
    if inc is None or inc.queue == "legacy":
        raise HTTPException(404, "incident not found")
    base = _serialise(db, [inc])[0]
    members = (db.query(IncidentMember, Alert, DetectionEvent)
                 .join(Alert, Alert.id == IncidentMember.alert_id)
                 .join(DetectionEvent, DetectionEvent.id == IncidentMember.event_id)
                 .filter(IncidentMember.incident_id == inc.id)
                 .order_by(DetectionEvent.timestamp, IncidentMember.id)
                 .all())
    timeline = [IncidentTimelineEntry(
        alert_id=a.id, event_id=ev.id, at=ev.timestamp, confidence=ev.confidence,
        snapshot_url=(f"/api/alerts/{a.id}/snapshot" if ev.thumbnail_path else None),
        notification_suppressed=bool(a.notification_suppressed),
    ) for _m, a, ev in members]
    transitions = [IncidentTransitionOut.model_validate(t) for t in
                   db.query(IncidentTransition)
                     .filter(IncidentTransition.incident_id == inc.id)
                     .order_by(IncidentTransition.id).all()]
    return IncidentDetail(**base.model_dump(), timeline=timeline,
                          transitions=transitions)


@router.post("/{incident_id}/{action}", response_model=IncidentOut)
def act_on_incident(incident_id: int, action: IncidentAction,
                    payload: IncidentActionIn | None = None,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_role("admin", "operator"))):
    """One action for the whole incident.

    dismiss records a "false" verdict on the head alert; resolve with
    verdict="true" records a "true" verdict. A verdict is review evidence
    for training, never a training trigger by itself.
    """
    payload = payload or IncidentActionIn()
    inc = db.query(Incident).filter(Incident.id == incident_id).with_for_update().first()
    if inc is None or inc.queue == "legacy":
        raise HTTPException(404, "incident not found")
    try:
        svc.apply_action(db, inc, action, actor_user_id=user.id,
                         reason=payload.reason, expected_version=payload.version)
    except svc.IncidentActionError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))

    head = db.get(Alert, inc.head_alert_id) if inc.head_alert_id else None
    if head is not None and payload.note and payload.note.strip():
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"[{when}] {user.email}: {payload.note.strip()}"
        head.notes = f"{head.notes}\n{line}" if head.notes else line
    verdict = ("dismiss" if action == "dismiss"
               else "confirm" if action == "resolve" and payload.verdict == "true"
               else None)
    if head is not None and verdict is not None:
        from app.services.alert_feedback import record_verdict
        # Commits the incident change together with the verdict.
        record_verdict(db, head.id, verdict, user)
        if action == "dismiss" and payload.reason:
            head = db.get(Alert, head.id)
            head.dismiss_reason = payload.reason[:32]
            db.commit()
    else:
        db.commit()
    db.refresh(inc)
    return _serialise(db, [inc])[0]
