"""Scheduled-report Celery tasks.

Beat schedule (declared in celery_app.py) wakes `dispatch_due_reports`
every 5 minutes. It checks every ScheduledReport row whose
`last_run_at` is older than the cadence (24h for daily, 168h for
weekly), renders the PDF/CSV via app.analytics.reports, and emails it
to recipients using the SMTP notifier creds.
"""
from __future__ import annotations
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


CADENCE_HOURS = {"daily": 24, "weekly": 24 * 7}


@celery_app.task(name="reports.dispatch_due", ignore_result=True)
def dispatch_due_reports() -> None:
    from app.database import SessionLocal
    from app.models import ScheduledReport
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        rows = db.query(ScheduledReport).filter(ScheduledReport.is_active == True).all()  # noqa: E712
        for r in rows:
            hours = CADENCE_HOURS.get(r.cadence, 24)
            if r.last_run_at and (now - r.last_run_at) < timedelta(hours=hours):
                continue
            dispatch_report.delay(r.id)
            log.info("reports.dispatch_due: queued report id=%s name=%s", r.id, r.name)


@celery_app.task(name="reports.dispatch_one", ignore_result=True)
def dispatch_report(report_id: int) -> None:
    from app.database import SessionLocal
    from app.models import ScheduledReport, Store
    from app.analytics.reports import store_rollup, stores_csv, stores_pdf

    with SessionLocal() as db:
        rep = db.get(ScheduledReport, report_id)
        if not rep:
            return
        hours = CADENCE_HOURS.get(rep.cadence, 24)
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        until = datetime.now(timezone.utc)

        if rep.store_id:
            ids = [rep.store_id]
            title = f"VivoGuard — store #{rep.store_id} ({rep.cadence})"
        else:
            ids = [s.id for s in db.query(Store).filter(Store.is_active == True).all()]  # noqa: E712
            title = f"VivoGuard — chain ({rep.cadence})"
        rollups = [store_rollup(db, sid, since=since, until=until) for sid in ids]

        if rep.format == "csv":
            payload = stores_csv(rollups)
            mime = "text/csv"
            ext  = "csv"
        else:
            payload = stores_pdf(title, rollups)
            mime = "application/pdf"
            ext  = "pdf"

        recipients = [r.strip() for r in (rep.recipients or "").split(",") if r.strip()]
        if not recipients or not settings.smtp_host:
            log.warning("dispatch_report: no recipients or no SMTP — skipping email for report id=%s", rep.id)
        else:
            _send_email(
                to=recipients, subject=title,
                body=f"Attached: {rep.cadence} {rep.format.upper()} report from VivoGuard AI.",
                attachment=payload, mime=mime,
                filename=f"vivoguard_{rep.cadence}.{ext}",
            )
            log.info("dispatch_report: emailed report id=%s to %s", rep.id, recipients)

        rep.last_run_at = datetime.now(timezone.utc)
        db.commit()


def _send_email(to: list[str], subject: str, body: str,
                attachment: bytes, mime: str, filename: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"]   = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    maintype, _, subtype = mime.partition("/")
    msg.add_attachment(attachment, maintype=maintype, subtype=subtype, filename=filename)

    if settings.smtp_use_tls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
