"""Schedulable reports — operators define a recurring report and the
Celery beat dispatcher fires it daily/weekly. PDFs and CSVs are emailed
via the existing SMTP notifier infrastructure.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from datetime import date as date_t, datetime, time as time_t

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import ScheduledReport

router = APIRouter(prefix="/reports/scheduled", tags=["reports"])


class ScheduledReportIn(BaseModel):
    name: str
    store_id: int | None = None
    cadence: str = "daily"     # daily | weekly | monthly | quarterly
    format:  str = "pdf"       # pdf | csv
    recipients: str            # comma-separated emails
    is_active: bool = True
    # Cron-precise dispatch. "21:00" = fire at 21:00 store-local;
    # null = legacy any-time behaviour.
    time_of_day: time_t | None = None
    # Only meaningful for cadence='weekly'. 0=Mon..6=Sun.
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    # Optional comma-separated `whatsapp:+<msisdn>` recipients.
    whatsapp_recipients: str | None = None


class ScheduledReportOut(ScheduledReportIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    last_run_at: datetime | None
    last_fire_date: date_t | None = None
    created_at: datetime


@router.get("", response_model=list[ScheduledReportOut])
def list_reports(db: Session = Depends(get_db), _u=Depends(get_current_user)):
    return db.query(ScheduledReport).order_by(ScheduledReport.id.desc()).all()


@router.post("", response_model=ScheduledReportOut)
def create(body: ScheduledReportIn, db: Session = Depends(get_db),
           _u=Depends(require_role("admin", "operator"))):
    if body.cadence not in ("daily", "weekly", "monthly", "quarterly"):
        raise HTTPException(400, "cadence must be daily | weekly | monthly | quarterly")
    if body.format not in ("pdf", "csv"):
        raise HTTPException(400, "format must be pdf or csv")
    r = ScheduledReport(**body.model_dump())
    db.add(r); db.commit(); db.refresh(r)
    return r


@router.delete("/{report_id}")
def remove(report_id: int, db: Session = Depends(get_db),
           _u=Depends(require_role("admin"))):
    r = db.get(ScheduledReport, report_id)
    if not r:
        raise HTTPException(404, "not found")
    db.delete(r); db.commit()
    return {"deleted": report_id}


@router.post("/{report_id}/run-now")
def run_now(report_id: int, db: Session = Depends(get_db),
            _u=Depends(require_role("admin", "operator"))):
    """Fire the report immediately (useful for testing). Enqueues the
    Celery task; returns when the task is queued."""
    if not db.get(ScheduledReport, report_id):
        raise HTTPException(404, "not found")
    from app.tasks.reports import dispatch_report
    dispatch_report.delay(report_id)
    return {"enqueued": report_id}
