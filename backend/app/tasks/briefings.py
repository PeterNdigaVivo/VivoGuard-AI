"""Morning WhatsApp briefings (Priority-3 of the May-2026 redesign).

Two scheduled tasks, both gated on a per-store wall-clock time:

  daily_briefing      08:00 store-local, every day. One message per
                      active store, sent to stores.manager_phone.
                      Headline-numbers summary from yesterday.

  weekly_briefing     07:00 store-local Monday. Chain-wide summary
                      for head-office recipients (set via the
                      WEEKLY_BRIEFING_TO env var, comma-separated
                      whatsapp:+... numbers).

Beat ticks every 5 minutes (see app/tasks/celery_app.py). Each task
checks "is now past the trigger time AND haven't fired today?". The
last-fire date is stored in Redis (vg:briefing:daily:{store_id}:date
+ vg:briefing:weekly:date) so duplicate firings within the tick
window are impossible.

Re-uses the existing Twilio integration (TWILIO_ACCOUNT_SID +
TWILIO_AUTH_TOKEN + TWILIO_WHATSAPP_FROM). Silent skip when those
env vars aren't set — dev environments just don't get briefings.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


DAILY_TRIGGER_HOUR  = 8     # 08:00 store-local
WEEKLY_TRIGGER_HOUR = 7     # 07:00 chain-time Monday


def _store_tz(store):
    tz_name = (getattr(store, "timezone", None) or "Africa/Nairobi")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _send_whatsapp(recipients: list[str], body: str) -> int:
    """WhatsApp delivery is disabled (Ops decision — dashboard alerts
    only). Kept as a no-op stub so every existing caller still
    compiles and the signature can be unwound piece-by-piece in
    follow-up commits. Logs once per call so the ops team can see
    that a notification WOULD have gone out."""
    if not recipients:
        return 0
    log.info("WhatsApp disabled — would have sent to %d recipient(s): %s",
             len(recipients), body[:120].replace("\n", " "))
    return 0


def _format_whatsapp_recipient(phone: str | None) -> str | None:
    """Twilio expects `whatsapp:+<msisdn>`. Operators often save just
    the digits — normalise on the way out."""
    if not phone:
        return None
    p = phone.strip()
    if p.startswith("whatsapp:"):
        return p
    if not p.startswith("+"):
        # heuristic: leading 0 → Kenya country code (+254). Operators
        # outside Kenya can save the full +<cc> form themselves.
        if p.startswith("0"):
            p = "+254" + p[1:]
        else:
            p = "+" + p
    return f"whatsapp:{p}"


# ----- Daily store briefing ------------------------------------------

@celery_app.task(name="briefings.daily_fire_due", ignore_result=True)
def daily_fire_due() -> None:
    """Beat-tick entry point. Walks active stores and fires the daily
    briefing for any whose store-local clock has crossed 08:00 and
    hasn't been briefed today."""
    from app.database import SessionLocal
    from app.models import Store

    r = _redis()
    now_utc = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for s in stores:
            local = now_utc.astimezone(_store_tz(s))
            today = local.date().isoformat()
            if local.hour < DAILY_TRIGGER_HOUR:
                continue
            key = f"vg:briefing:daily:{s.id}:date"
            last = r.get(key)
            if last == today:
                continue       # already briefed today
            try:
                _send_daily_for_store(db, s)
                r.set(key, today, ex=2 * 24 * 3600)
            except Exception as e:
                log.exception("daily briefing failed for store %s: %s", s.id, e)


def _send_daily_for_store(db, store) -> None:
    """Compose + send the per-store summary. Called from the dispatcher
    once per store per day."""
    from app.models import Alert, Camera, DetectionEvent, VisitorTrack
    from sqlalchemy import func
    cam_ids = [c.id for c in db.query(Camera).filter(Camera.store_id == store.id).all()]
    if not cam_ids:
        log.info("daily briefing: store %s has no cameras — skipping", store.id)
        return

    # Yesterday's window in store-local terms.
    local = datetime.now(timezone.utc).astimezone(_store_tz(store))
    today_local_00  = local.replace(hour=0, minute=0, second=0, microsecond=0)
    yest_local_00   = today_local_00 - timedelta(days=1)
    yest_open_utc   = yest_local_00.astimezone(timezone.utc)
    yest_close_utc  = today_local_00.astimezone(timezone.utc)

    # Yesterday visitors.
    visitors = (db.query(func.count(VisitorTrack.id))
                  .filter(VisitorTrack.store_id == store.id,
                          VisitorTrack.first_seen >= yest_open_utc,
                          VisitorTrack.first_seen <  yest_close_utc).scalar() or 0)
    # Same day last week.
    lw_open  = yest_open_utc  - timedelta(days=7)
    lw_close = yest_close_utc - timedelta(days=7)
    lw_visitors = (db.query(func.count(VisitorTrack.id))
                     .filter(VisitorTrack.store_id == store.id,
                             VisitorTrack.first_seen >= lw_open,
                             VisitorTrack.first_seen <  lw_close).scalar() or 0)
    delta_pct = None
    if lw_visitors:
        delta_pct = round((visitors - lw_visitors) / lw_visitors * 100, 1)

    # Avg queue wait (sec) + avg staff presence (0..1) — both as
    # MetricSnapshot averages over the same window.
    from app.models import MetricSnapshot
    q_avg = (db.query(func.avg(MetricSnapshot.value))
               .filter(((MetricSnapshot.store_id == store.id)
                        | MetricSnapshot.camera_id.in_(cam_ids)),
                       MetricSnapshot.metric_type == "queue_wait_seconds",
                       MetricSnapshot.period_start >= yest_open_utc,
                       MetricSnapshot.period_start <  yest_close_utc).scalar() or 0)
    staff_avg = (db.query(func.avg(MetricSnapshot.value))
                   .filter(((MetricSnapshot.store_id == store.id)
                            | MetricSnapshot.camera_id.in_(cam_ids)),
                           MetricSnapshot.metric_type == "staff_present_pct",
                           MetricSnapshot.period_start >= yest_open_utc,
                           MetricSnapshot.period_start <  yest_close_utc).scalar() or 0)

    # Incidents — count yesterday's alerts, split confirmed vs new.
    incidents = (db.query(Alert.status, func.count(Alert.id))
                   .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                   .filter(DetectionEvent.camera_id.in_(cam_ids),
                           Alert.created_at >= yest_open_utc,
                           Alert.created_at <  yest_close_utc)
                   .group_by(Alert.status).all())
    inc = {row[0]: int(row[1]) for row in incidents}
    inc_total   = sum(inc.values())
    inc_pending = inc.get("new", 0)
    inc_handled = inc.get("confirmed", 0) + inc.get("dismissed", 0)

    # Busiest hour — bucket yesterday's per-hour MetricSnapshot sum.
    from sqlalchemy import extract
    rows = (db.query(extract("hour", MetricSnapshot.period_start).label("h"),
                     func.sum(MetricSnapshot.value).label("v"))
              .filter(((MetricSnapshot.store_id == store.id)
                       | MetricSnapshot.camera_id.in_(cam_ids)),
                      MetricSnapshot.metric_type == "visitor_count_in",
                      MetricSnapshot.period_start >= yest_open_utc,
                      MetricSnapshot.period_start <  yest_close_utc)
              .group_by("h").all())
    if rows:
        peak = max(rows, key=lambda r: float(r.v or 0))
        peak_line = (f"🔥 Busiest: {int(peak.h):02d}:00-{int(peak.h)+1:02d}:00 "
                     f"({int(peak.v or 0)} customers)")
    else:
        peak_line = "🔥 Busiest hour: data unavailable"

    delta_str = (f"({'↑' if (delta_pct or 0) > 0 else '↓' if (delta_pct or 0) < 0 else '→'} "
                 f"{abs(delta_pct)}% vs same day last week)") if delta_pct is not None else ""
    wait_min = round(q_avg / 60, 1) if q_avg else 0
    wait_emoji = "✅" if wait_min <= 3 else "⚠️"
    staff_pct = round(float(staff_avg) * 100)
    staff_emoji = "✅" if staff_pct >= 90 else "⚠️"

    name = (store.manager_name or "Manager").split()[0]   # first name only
    body = (
        f"Good morning {name}! Yesterday {store.name} had:\n"
        f"📊 {visitors} customers {delta_str}\n"
        f"⏱️ Avg wait: {wait_min} min {wait_emoji}\n"
        f"👥 Staff presence: {staff_pct}% {staff_emoji}\n"
        f"🚨 {inc_total} incidents ({inc_handled} resolved, {inc_pending} pending)\n"
        f"{peak_line}"
    ).strip()

    # Recipients — store.manager_phone is the canonical column. Fall
    # back silently when blank.
    recipient = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
    if not recipient:
        log.info("daily briefing: store %s has no manager_phone — skipping send", store.id)
        return
    sent = _send_whatsapp([recipient], body)
    log.info("daily briefing: store %s — sent %d (%s)", store.id, sent, recipient)


# ----- Weekly chain briefing -----------------------------------------

@celery_app.task(name="briefings.weekly_fire_due", ignore_result=True)
def weekly_fire_due() -> None:
    """Fires Monday 07:00 chain-time. Chain-time = the first store's
    timezone in alphabetical order — close enough for an internal
    summary."""
    from app.database import SessionLocal
    from app.models import Store

    r = _redis()
    now_utc = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).order_by(Store.name).all()  # noqa: E712
        if not stores:
            return
        anchor = stores[0]
        local = now_utc.astimezone(_store_tz(anchor))
        if local.weekday() != 0:        # 0 = Monday
            return
        if local.hour < WEEKLY_TRIGGER_HOUR:
            return
        iso_year, iso_week, _ = local.isocalendar()
        key = "vg:briefing:weekly:date"
        marker = f"{iso_year}-W{iso_week:02d}"
        if r.get(key) == marker:
            return       # already sent this week
        try:
            _send_weekly_for_chain(db, stores)
            r.set(key, marker, ex=14 * 24 * 3600)
        except Exception as e:
            log.exception("weekly briefing failed: %s", e)


def _send_weekly_for_chain(db, stores) -> None:
    """Chain-wide weekly summary. Recipients come from the
    WEEKLY_BRIEFING_TO env var (comma-separated whatsapp:+... numbers)."""
    from app.models import Alert, DetectionEvent, VisitorTrack
    from sqlalchemy import func
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)

    total_visitors = (db.query(func.count(VisitorTrack.id))
                        .filter(VisitorTrack.first_seen >= week_start).scalar() or 0)
    total_alerts   = (db.query(func.count(Alert.id))
                        .filter(Alert.created_at >= week_start).scalar() or 0)
    crit_types = ("intrusion", "fight", "weapon", "weapon_brandished", "shrinkage", "fire")
    critical_alerts = (db.query(func.count(Alert.id))
                         .join(DetectionEvent, Alert.event_id == DetectionEvent.id)
                         .filter(Alert.created_at >= week_start,
                                 DetectionEvent.detection_type.in_(crit_types)).scalar() or 0)
    # Best-performing store: highest visitor count this week.
    best = None; best_v = 0
    for s in stores:
        v = (db.query(func.count(VisitorTrack.id))
               .filter(VisitorTrack.store_id == s.id,
                       VisitorTrack.first_seen >= week_start).scalar() or 0)
        if v > best_v:
            best_v = v; best = s.name

    body = (
        "📊 Weekly chain summary\n"
        f"Stores: {len(stores)}\n"
        f"Visitors (7d): {total_visitors}\n"
        f"Alerts (7d): {total_alerts}\n"
        f"Critical alerts: {critical_alerts}\n"
    )
    if best:
        body += f"🏆 Best store: {best} ({best_v} visitors)\n"

    raw = (getattr(settings, "weekly_briefing_to", "") or "")
    recipients = [
        _format_whatsapp_recipient(t.strip())
        for t in raw.split(",")
        if t.strip()
    ]
    recipients = [r for r in recipients if r]
    if not recipients:
        log.info("weekly briefing: no WEEKLY_BRIEFING_TO recipients configured")
        return
    sent = _send_whatsapp(recipients, body)
    log.info("weekly briefing: sent to %d / %d recipients", sent, len(recipients))
