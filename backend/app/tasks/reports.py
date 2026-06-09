"""Scheduled-report Celery tasks.

Beat tick (celery_app.py) fires `dispatch_due_reports` every 5
minutes. The dispatcher walks the active ScheduledReport rows and
decides — per row — whether NOW is "the right minute".

Two firing modes:

  1. Cron-precise (time_of_day set):
     • daily : fire when local clock has just passed time_of_day
                AND last_fire_date != today (store-local).
     • weekly: same, but also today's weekday must equal day_of_week.
     The 5-minute beat tick is the resolution — a 21:00 daily report
     can fire anywhere in 21:00..21:04, never twice in the same day.
     `last_fire_date` (a DATE, not a timestamp) is the dedup key.

  2. Legacy (time_of_day NULL):
     • Fire whenever last_run_at is older than the cadence's hours.
     Kept for backwards compatibility with rows created before the
     0009 migration. New rows from the UI default to cron-precise.

Window math:
  • Report covers exactly the period the operator probably means.
  • Daily fired at 21:00 → window = today 00:00 → 21:00 local.
  • Weekly fired Monday 08:00 → window = last Monday 00:00 → this
    Monday 00:00 local.
  • All windows further clamped to business hours when the report is
    store-scoped — so after-hours noise doesn't make it into the PDF.

Delivery:
  • Email via SMTP notifier (existing path).
  • WhatsApp side-channel via Twilio when whatsapp_recipients is set —
    plain-text message naming the file + the headline number. Twilio
    can't take a binary attachment without a public URL; we surface a
    short summary instead so on-call managers see the headline number
    on their phone without opening the PDF.
"""
from __future__ import annotations
import logging
import smtplib
from datetime import date as date_t, datetime, time as time_t, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


CADENCE_HOURS = {
    "daily":     24,
    "weekly":    24 * 7,
    "monthly":   24 * 30,
    "quarterly": 24 * 90,
}


def _store_tz(store):
    """Resolve a store's timezone (with a Vivo-default fallback). Wrap
    in a helper so we don't repeat the ZoneInfo import each call."""
    tz_name = (getattr(store, "timezone", None) or "Africa/Nairobi") if store else "Africa/Nairobi"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _local_now(store) -> datetime:
    """Wall-clock now in the store's timezone. Used for the cron
    comparison so 21:00 = 21:00 Nairobi, not 21:00 UTC."""
    return datetime.now(timezone.utc).astimezone(_store_tz(store))


def _should_fire(rep, db) -> bool:
    """Decide whether `rep` should fire on this beat tick.

    See module docstring for the rules. Returns True iff the row
    should be dispatched RIGHT NOW.
    """
    if not rep.is_active:
        return False

    # Legacy any-time mode: keep the old behaviour (fire when older
    # than cadence-hours). Lets pre-migration rows continue working.
    if rep.time_of_day is None:
        hours = CADENCE_HOURS.get(rep.cadence, 24)
        if rep.last_run_at is None:
            return True
        return (datetime.now(timezone.utc) - rep.last_run_at) >= timedelta(hours=hours)

    # Cron-precise mode.
    store = None
    if rep.store_id:
        from app.models import Store
        store = db.get(Store, rep.store_id)
    local = _local_now(store)

    # Weekly: only the configured weekday is eligible. day_of_week
    # uses python convention (0=Mon..6=Sun).
    if rep.cadence == "weekly":
        if rep.day_of_week is None:
            # Operator picked weekly but didn't set day_of_week;
            # default to Monday.
            target_dow = 0
        else:
            target_dow = rep.day_of_week
        if local.weekday() != target_dow:
            return False

    # Has today's fire window already opened?
    if local.time() < rep.time_of_day:
        return False

    # And we haven't fired today already.
    if rep.last_fire_date == local.date():
        return False

    return True


@celery_app.task(name="reports.dispatch_due", ignore_result=True)
def dispatch_due_reports() -> None:
    from app.database import SessionLocal
    from app.models import ScheduledReport
    with SessionLocal() as db:
        rows = db.query(ScheduledReport).all()
        for r in rows:
            if _should_fire(r, db):
                dispatch_report.delay(r.id)
                log.info(
                    "reports.dispatch_due: queued report id=%s name=%s cadence=%s "
                    "time_of_day=%s dow=%s",
                    r.id, r.name, r.cadence, r.time_of_day, r.day_of_week,
                )


def _report_window(rep, store) -> tuple[datetime, datetime]:
    """Window covered by the report.

    Daily cron-precise mode clamps to the store's business-hours
    SESSION (09:00–21:00 by default) so the PDF doesn't include
    cleaner-shift noise. Weekly mode covers the previous 7 days at
    midnight boundaries — fine-grained business-hours per day would
    require per-day SQL filters, and the noise impact is small at
    that aggregate level.

    Legacy mode (time_of_day NULL) keeps the rolling-N-hours behaviour.
    """
    if rep.time_of_day is None:
        hours = CADENCE_HOURS.get(rep.cadence, 24)
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=hours)
        return since, until

    if rep.cadence == "weekly":
        local = _local_now(store)
        end_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = end_local - timedelta(days=7)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    # Daily — clamp to business hours when we have a single store.
    if store is not None:
        from app.utils.business_hours import todays_session
        return todays_session(store)

    # Chain-wide daily (rep.store_id is None): widest reasonable
    # window — local 00:00 to now-ish. Per-store business-hours
    # clamping inside the rollup loop would be a follow-up.
    local = _local_now(None)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc),
            local.astimezone(timezone.utc))


@celery_app.task(name="reports.dispatch_one", ignore_result=True)
def dispatch_report(report_id: int) -> None:
    from app.database import SessionLocal
    from app.models import ScheduledReport, Store
    from app.analytics.reports import store_rollup, stores_csv, stores_pdf

    with SessionLocal() as db:
        rep = db.get(ScheduledReport, report_id)
        if not rep:
            return

        store: Optional[object] = None
        if rep.store_id:
            store = db.get(Store, rep.store_id)
        since, until = _report_window(rep, store)

        if rep.store_id:
            ids = [rep.store_id]
            title = f"VivoGuard — {store.name if store else 'store'} ({rep.cadence})"
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
        filename = f"vivoguard_{rep.cadence}.{ext}"

        # Email — existing path.
        recipients = [r.strip() for r in (rep.recipients or "").split(",") if r.strip()]
        if not recipients or not settings.smtp_host:
            log.warning("dispatch_report: no recipients or no SMTP — skipping email for report id=%s", rep.id)
        else:
            try:
                _send_email(
                    to=recipients, subject=title,
                    body=f"Attached: {rep.cadence} {rep.format.upper()} report from VivoGuard AI.\n"
                         f"Window: {since.isoformat()} → {until.isoformat()}",
                    attachment=payload, mime=mime, filename=filename,
                )
                log.info("dispatch_report: emailed report id=%s to %s", rep.id, recipients)
            except Exception as e:
                log.warning("dispatch_report: email failed for report id=%s: %s", rep.id, e)

        # WhatsApp side-channel. Twilio can't attach binary without a
        # public URL, so we send a short summary that gives the
        # on-call manager the headline number on their phone.
        wa_to = [r.strip() for r in (rep.whatsapp_recipients or "").split(",") if r.strip()]
        if wa_to:
            try:
                _send_whatsapp_summary(rep, store, rollups, wa_to)
                log.info("dispatch_report: WhatsApp sent for report id=%s to %s", rep.id, wa_to)
            except Exception as e:
                log.warning("dispatch_report: WhatsApp failed for report id=%s: %s", rep.id, e)

        # Update fire tracking. last_fire_date dedups inside today's
        # 5-minute beat window; last_run_at supports the legacy
        # rolling-hours fallback.
        if rep.time_of_day is not None:
            local = _local_now(store)
            rep.last_fire_date = local.date()
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


def _send_whatsapp_summary(rep, store, rollups: list[dict],
                            recipients: list[str]) -> None:
    """WhatsApp delivery is disabled (Ops decision — dashboard alerts
    only). Kept as a no-op so the dispatch loop signature stays the
    same; email and dashboard alerts are unaffected."""
    if recipients:
        log.info("WhatsApp disabled — would have sent %s summary to %d",
                 getattr(rep, "cadence", "report"), len(recipients))
