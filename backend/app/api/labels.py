"""Labelling Sprint endpoints — Part 5.

The sprint UI is a fast-review queue of unlabelled alerts. Operators
keyboard-tap C/D/← to label or undo. Endpoints:

  GET    /labels/queue              — next batch of unlabelled alerts (≤20)
  POST   /labels/{alert_id}         — record verdict (calls record_verdict)
  DELETE /labels/{alert_id}/undo    — reverse verdict (calls revert_verdict)
  GET    /labels/today              — progress counters for the header bar

All four are thin — heavy lifting lives in services/alert_feedback +
training/feedback_loop. No duplication of the confirm/dismiss logic
(Rule #2 of the multi-part spec).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import (
    Alert, AlertReviewDecision, Camera, DetectionEvent, Store, User, Zone,
)
from app.schemas.alert import AlertActionOut, AlertOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/labels", tags=["labels"])

# Per Part 5 spec: 20 alerts per session.
DEFAULT_BATCH = 20
MAX_BATCH     = 50

CRITICAL_REVIEW_TYPES = {
    "weapon", "weapon_brandished", "fight", "fire", "smoke", "fall",
}
HIGH_REVIEW_TYPES = {
    "intrusion", "trespass", "staff_zone", "tailgating", "shrinkage",
    "stockroom_access", "shop_open_close",
}


def _review_priority(detection_type: str) -> tuple[int, str]:
    if detection_type in CRITICAL_REVIEW_TYPES:
        return 0, "critical life-safety review"
    if detection_type in HIGH_REVIEW_TYPES:
        return 1, "high-risk operational review"
    return 2, "routine alert-quality review"


class VerdictIn(BaseModel):
    verdict: Literal["confirm", "dismiss"]


class UndoOut(BaseModel):
    undone: bool
    training_image_removed: bool


class TodayCountsOut(BaseModel):
    reviewed_today: int       # confirmed + dismissed today (EAT)
    queue_total:    int       # unreviewed status='new' alerts (no filters)


def _dwell_from_event(ev: Optional[DetectionEvent]) -> Optional[int]:
    """Single source of truth for dwell — DetectionEvent.extra['dwell_seconds'].
    Returns int seconds, or None when the alert type doesn't carry one
    (sprint card renders 'N/A')."""
    if ev is None or not ev.extra:
        return None
    v = ev.extra.get("dwell_seconds")
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _serialise_queue_rows(
    db: Session, rows: list[tuple], *, blind: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Build review cards from evidence that still exists on this worker.

    A verdict without viewable evidence is not a human validation. Database
    paths can outlive the retention window, so fail closed here instead of
    showing a black card that an operator can still confirm or dismiss.
    Blind audit cards also never reveal the first verdict.
    """
    zone_ids = {ev.zone_id for _, ev, _ in rows if ev.zone_id is not None}
    store_ids = {cam.store_id for _, _, cam in rows
                 if cam and cam.store_id is not None}
    zones_by_id = ({z.id: z for z in db.query(Zone)
                                      .filter(Zone.id.in_(zone_ids)).all()}
                   if zone_ids else {})
    stores_by_id = ({s.id: s for s in db.query(Store)
                                       .filter(Store.id.in_(store_ids)).all()}
                    if store_ids else {})
    from app.api.alerts import _to_alert_out

    out: list[dict] = []
    for alert, event, camera in rows:
        clip_path = (event.extra or {}).get("alert_clip_path")
        snapshot_exists = bool(
            event.thumbnail_path and Path(event.thumbnail_path).is_file()
        )
        clip_exists = bool(clip_path and Path(str(clip_path)).is_file())
        if not snapshot_exists and not clip_exists:
            continue
        item: AlertOut = _to_alert_out(
            alert, event, camera,
            zones_by_id.get(event.zone_id) if event.zone_id else None,
            stores_by_id.get(camera.store_id)
            if (camera and camera.store_id) else None,
        )
        payload = item.model_dump()
        payload["snapshot_url"] = (
            f"/api/alerts/{alert.id}/snapshot" if snapshot_exists else None
        )
        payload["clip_url"] = (
            f"/api/alerts/{alert.id}/clip" if clip_exists else None
        )
        if blind:
            payload["status"] = "independent_review_pending"
        payload["dwell_seconds"] = _dwell_from_event(event)
        rank, reason = _review_priority(event.detection_type)
        payload["review_priority"] = rank
        payload["review_reason"] = (
            "blind independent review" if blind else reason
        )
        payload["clip_available"] = clip_exists
        out.append(payload)
        if limit is not None and len(out) >= limit:
            break
    return out


# ---- GET /labels/queue ----------------------------------------------

@router.get("/queue", response_model=list[dict])
def queue(
    db:    Session = Depends(get_db),
    _user: User    = Depends(get_current_user),
    limit:          int = Query(DEFAULT_BATCH, ge=1, le=MAX_BATCH),
    detection_type: Optional[str] = Query(None,
        description="Filter to one detector (e.g. 'checkout_dwell')"),
    store_id:       Optional[int] = Query(None,
        description="Filter via Camera.store_id (Alert has no store column)"),
    camera_id:      Optional[int] = Query(None,
        description="Pin a validation batch to one exact camera"),
):
    """Next batch of unlabelled alerts, newest-first.

    Unlabelled = `status='new' AND feedback_used_for_training=False`.
    Mirrors /alerts list endpoint's JOIN pattern so the full
    _to_alert_out payload (camera name, store, zone, snapshot URL)
    comes back, plus a top-level `dwell_seconds` field spliced from
    DetectionEvent.extra for the sprint card."""
    q = (db.query(Alert, DetectionEvent, Camera)
           .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
           .outerjoin(Camera, DetectionEvent.camera_id == Camera.id)
           .filter(Alert.status == "new")
           .filter(DetectionEvent.detection_type != "positive_operational")
           .filter(Alert.feedback_used_for_training == False))   # noqa: E712
    if detection_type:
        q = q.filter(DetectionEvent.detection_type == detection_type)
    if store_id is not None:
        q = q.filter(Camera.store_id == store_id)
    if camera_id is not None:
        q = q.filter(DetectionEvent.camera_id == camera_id)
    # Avoid scanning the entire historical queue. This database-level filter
    # is only a candidate filter; _serialise_queue_rows still checks that the
    # retained file exists before exposing the card.
    q = q.filter(or_(
        DetectionEvent.thumbnail_path.is_not(None),
        DetectionEvent.extra["alert_clip_path"].as_string().is_not(None),
    ))
    priority_rank = case(
        (DetectionEvent.detection_type.in_(CRITICAL_REVIEW_TYPES), 0),
        (DetectionEvent.detection_type.in_(HIGH_REVIEW_TYPES), 1),
        else_=2,
    )
    # Consequential alerts first; within a tier review the oldest outstanding
    # evidence first so a stream of new routine alerts cannot starve the SLA.
    q = q.order_by(priority_rank.asc(), Alert.created_at.asc()).limit(limit * 5)
    rows = q.all()

    return _serialise_queue_rows(db, rows, limit=limit)


@router.get("/audit-queue", response_model=list[dict])
def audit_queue(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(DEFAULT_BATCH, ge=1, le=MAX_BATCH),
):
    """Blind second-review sample, excluding the primary reviewer."""
    reviewed_by_other = exists().where(and_(
        AlertReviewDecision.alert_id == Alert.id,
        AlertReviewDecision.reviewer_id != user.id,
    ))
    reviewed_by_user = exists().where(and_(
        AlertReviewDecision.alert_id == Alert.id,
        AlertReviewDecision.reviewer_id == user.id,
    ))
    priority_rank = case(
        (DetectionEvent.detection_type.in_(CRITICAL_REVIEW_TYPES), 0),
        (DetectionEvent.detection_type.in_(HIGH_REVIEW_TYPES), 1),
        else_=2,
    )
    rows = (db.query(Alert, DetectionEvent, Camera)
              .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
              .outerjoin(Camera, DetectionEvent.camera_id == Camera.id)
              .filter(Alert.status.in_(("confirmed", "dismissed")))
              .filter(DetectionEvent.detection_type != "positive_operational")
              .filter(or_(
                  DetectionEvent.thumbnail_path.is_not(None),
                  DetectionEvent.extra["alert_clip_path"].as_string().is_not(None),
              ))
              .filter(reviewed_by_other)
              .filter(~reviewed_by_user)
              .order_by(priority_rank.asc(), Alert.created_at.asc())
              .limit(limit * 5).all())
    return _serialise_queue_rows(db, rows, blind=True, limit=limit)


@router.post("/{alert_id}/audit", response_model=dict)
def audit_label(
    alert_id: int,
    body: VerdictIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "operator")),
):
    from app.services.alert_feedback import record_independent_verdict
    return record_independent_verdict(db, alert_id, body.verdict, user)


# ---- POST /labels/{alert_id} ----------------------------------------

@router.post("/{alert_id}", response_model=AlertActionOut)
def label(
    alert_id: int,
    body: VerdictIn,
    db: Session  = Depends(get_db),
    user: User   = Depends(require_role("admin", "operator")),
):
    """Record a sprint verdict. Thin wrapper around the Part 3
    service so the feedback-loop side effect is identical to the
    /alerts/{id}/confirm and /dismiss endpoints (Rule #2)."""
    from app.services.alert_feedback import record_verdict
    # Pre-fetch the joined event so record_verdict has it without a
    # second round-trip (Part 5 locked-in signature).
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "alert not found")
    ev = db.get(DetectionEvent, a.event_id) if a.event_id else None
    return record_verdict(db, alert_id, body.verdict, user, event=ev)


# ---- DELETE /labels/{alert_id}/undo ---------------------------------

@router.delete("/{alert_id}/undo", response_model=UndoOut)
def undo(
    alert_id: int,
    db: Session  = Depends(get_db),
    user: User   = Depends(require_role("admin", "operator")),
):
    """Reverse the operator's last verdict. Resets alert.status back
    to 'new' so the alert re-enters the sprint queue, and deletes
    the TrainingImage the verdict created (when found — pre-
    migration-0025 alerts have NULL source_alert_id and can't be
    cleaned up; we still flip the status + feedback flag)."""
    from app.training.feedback_loop import revert_verdict
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "alert not found")
    if a.status not in ("confirmed", "dismissed"):
        raise HTTPException(409,
            f"cannot undo alert in status '{a.status}' "
            f"(expected 'confirmed' or 'dismissed')")
    image_removed = revert_verdict(db, alert_id)
    # revert_verdict already cleared feedback_used_for_training; we
    # flip the status back to 'new' so it re-enters the queue.
    a = db.get(Alert, alert_id)
    if a is not None:
        a.status         = "new"
        a.acknowledged_at = None
        a.assigned_to    = None
        db.commit()
    log.info("sprint undo: alert=%s image_removed=%s user=%s",
             alert_id, image_removed, user.id)
    return UndoOut(undone=True, training_image_removed=image_removed)


# ---- GET /labels/today ----------------------------------------------

@router.get("/today", response_model=TodayCountsOut)
def today_counts(
    db:    Session = Depends(get_db),
    _user: User    = Depends(get_current_user),
):
    """Header-bar progress counters. EAT calendar day."""
    from zoneinfo import ZoneInfo
    eat = ZoneInfo("Africa/Nairobi")
    now = datetime.now(timezone.utc)
    today_eat_midnight = (now.astimezone(eat)
                              .replace(hour=0, minute=0, second=0, microsecond=0))
    start_utc = today_eat_midnight.astimezone(timezone.utc)

    reviewed = (db.query(func.count(Alert.id))
                  .filter(Alert.acknowledged_at >= start_utc)
                  .filter(Alert.status.in_(("confirmed", "dismissed")))
                  .scalar() or 0)
    pending  = (db.query(func.count(Alert.id))
                  .filter(Alert.status == "new")
                  .filter(Alert.feedback_used_for_training == False)   # noqa: E712
                  .scalar() or 0)
    return TodayCountsOut(reviewed_today=int(reviewed), queue_total=int(pending))
