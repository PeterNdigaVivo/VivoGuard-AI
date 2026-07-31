"""Daily Queue Intelligence report (WhatsApp).

Beat-tick: every 5 minutes scan active stores; when local clock has
crossed 21:00 and we haven't already sent today, build a plain-English
queue performance summary for that store and WhatsApp it to the
manager (falling back to the ops dashboard number). Deduped per
store per day in Redis.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Hour-of-day (store-local) at which the report fires.
SEND_HOUR = 21


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _store_tz(store):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(getattr(store, "timezone", None) or "Africa/Nairobi")
    except Exception:
        return timezone.utc


def _fmt_mmss(seconds) -> str:
    s = int(round(float(seconds or 0)))
    return f"{s // 60} min {s % 60:02d} sec"


@celery_app.task(name="queue_report.fire_due", ignore_result=True)
def queue_report_fire_due() -> None:
    """Beat entry — fires the daily queue report once per active store
    once its local clock has crossed 21:00."""
    from app.database import SessionLocal
    from app.models import Store

    r = _redis()
    now_utc = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for s in stores:
            local = now_utc.astimezone(_store_tz(s))
            today = local.date().isoformat()
            if local.hour < SEND_HOUR:
                continue
            key = f"vg:queue_report:daily:{s.id}:date"
            if r.get(key) == today:
                continue   # already sent today
            try:
                _send_queue_report(db, s, today_local=local)
                r.set(key, today, ex=2 * 24 * 3600)
            except Exception as e:
                log.exception("queue report failed for store %s: %s", s.id, e)


def _send_queue_report(db, store, *, today_local: datetime) -> None:
    """Compose + WhatsApp the daily queue performance summary."""
    from app.api.analytics import store_queue_intelligence
    # Re-use the endpoint logic so the WhatsApp numbers always match
    # what the dashboard shows. Pass a fake `_u` and the same db.
    payload = store_queue_intelligence(
        store.id, date=today_local.date().isoformat(), db=db, _u=None,
    )
    summary = payload.get("summary")
    sla = payload.get("sla")
    if not summary or not sla:
        log.info("queue report: store %s has no data today — skipping", store.id)
        return

    weekday = today_local.strftime("%A %d %B")
    avg = _fmt_mmss(summary["avg_wait_seconds"])
    target = _fmt_mmss(sla["target_max_wait_seconds"])
    breach_pct = sla["breach_pct"]
    peak_hour = payload.get("peak_hour") or "—"
    quietest = payload.get("quietest_hour") or "—"
    recs = payload.get("recommendations") or []
    rec_line = (recs[0] if recs else
                "Keep an eye on peak hours and add a till if waits creep up.")

    body = (
        f"📊 *Queue Report — {store.name} — {weekday}*\n\n"
        f"Today's checkout performance:\n"
        f"⏱️ Avg wait: {avg} (target: under {target})\n"
        f"🔴 Long waits (over target): {breach_pct}% of customers\n"
        f"🏆 Best hour: {quietest}\n"
        f"⚠️ Worst hour: {peak_hour}\n\n"
        f"*Recommendation for tomorrow:*\n{rec_line}\n\n"
        f"Full report: /stores/{store.id}"
    )

    from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
    recipients: list[str] = []
    mgr = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
    if mgr:
        recipients.append(mgr)
    # Fallback to the ops dashboard number so head office gets it too.
    raw = (getattr(settings, "dashboard_alert_to", "") or "").split(",")
    for part in raw:
        norm = _format_whatsapp_recipient(part.strip())
        if norm and norm not in recipients:
            recipients.append(norm)
    sent = _send_whatsapp(recipients, body)
    log.info("queue report: %s → %d WhatsApp sent", store.name, sent)
