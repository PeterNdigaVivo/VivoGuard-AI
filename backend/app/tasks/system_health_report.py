"""VivoGuard Status Report — the ONE daily email, 11:30 EAT.

Replaces the old "system health report" email entirely. Sections:
  1. Total Visitors (yesterday, per store, sorted)
  2. Alerts summary (yesterday, three plain severity buckets)
  3. Busiest hour chain-wide (yesterday)
  4. AI training feedback (yesterday's True/False clicks)
  5. System health (live at send time, ✅/⚠️/🔴 plain language)

Design rule: readable by a non-technical manager. No IDs, no JSON,
no tracebacks — a data section that fails to collect degrades to one
friendly line ("⚠️ Some stats unavailable this morning.").

Delivery: 5-min tick on the `beat` queue, which has its OWN dedicated
1-slot worker process (compose: beat-runner inside worker-alerts) so
long training jobs on `alerts` can never starve it again. Fires once
inside 11:30-11:45 EAT,
Redis SET-NX day marker AFTER a successful send; an SMTP failure
retries every 15 min up to 4 times (retries bypass the clock gate but
not the sent marker). Send and skip are both logged at INFO.

Manual test send:  daily_status_report.delay(force=True)
"""
from __future__ import annotations

import html as _html
import logging
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from app.config import settings
from app.tasks.celery_app import celery_app
from app.utils.system_admins import SYSTEM_ADMIN_EMAILS

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")
_FIRE_HOUR = 11             # 11:30 EAT
_FIRE_MINUTE = 30
_FIRE_WINDOW_MIN = 15       # fire anywhere in 11:30-11:45 (5-min tick)
_RETRY_COUNTDOWN_S = 15 * 60
_MAX_RETRIES = 4
_SENT_TTL_S = 20 * 60 * 60


def _sent_key(day_iso: str) -> str:
    return f"vg:statusreport:sent:{day_iso}"


def _lock_key(day_iso: str) -> str:
    return f"vg:statusreport:lock:{day_iso}"


# ── data collection (yesterday, EAT) ─────────────────────────────────


def _yesterday_window():
    """(label, since_utc, until_utc) for yesterday's FULL EAT day."""
    today_eat = datetime.now(EAT).replace(hour=0, minute=0, second=0,
                                          microsecond=0)
    y_start = today_eat - timedelta(days=1)
    label = y_start.strftime("%A, %d %B %Y")
    return (label,
            y_start.astimezone(timezone.utc),
            today_eat.astimezone(timezone.utc))


def _collect_yesterday(db) -> dict:
    """All yesterday-scoped stats. Each block degrades independently —
    a missing block sets its key to None and flips `degraded`."""
    from sqlalchemy import func
    from app.models import Alert, DetectionEvent, MetricSnapshot, Store

    label, since, until = _yesterday_window()
    out: dict = {"label": label, "degraded": False,
                 "visitors": None, "alerts": None,
                 "peak": None, "feedback": None}

    try:   # 1) visitors per store (entry crossings)
        from app.utils.analytics_queries import fetch_metric_aggregates
        stores = db.query(Store).all()
        _avg, sums = fetch_metric_aggregates(
            db, [s.id for s in stores], since, until)
        rows = sorted(
            ((s.name, int(sums.get((s.id, "visitor_count_in"), 0)))
             for s in stores),
            key=lambda kv: -kv[1])
        out["visitors"] = {"total": sum(n for _, n in rows), "rows": rows}
    except Exception:
        log.error("status report: visitors block failed:\n%s",
                  traceback.format_exc())
        out["degraded"] = True

    try:   # 2) alerts by plain severity bucket
        from app.api.alerts import _SEVERITY_LABEL
        buckets = {"URGENT": 0, "ATTENTION": 0, "INFO": 0}
        for dt, n in (db.query(DetectionEvent.detection_type,
                               func.count(Alert.id))
                        .join(DetectionEvent,
                              Alert.event_id == DetectionEvent.id)
                        .filter(Alert.created_at >= since,
                                Alert.created_at < until)
                        .group_by(DetectionEvent.detection_type).all()):
            buckets[_SEVERITY_LABEL.get(dt or "", "INFO")] += int(n)
        out["alerts"] = buckets
    except Exception:
        log.error("status report: alerts block failed:\n%s",
                  traceback.format_exc())
        out["degraded"] = True

    try:   # 3) busiest hour chain-wide (entry crossings per hour)
        hour_bucket = func.date_trunc(
            "hour", MetricSnapshot.period_start).label("hour_bucket")
        rows = (db.query(hour_bucket, func.sum(MetricSnapshot.value))
                  .filter(MetricSnapshot.metric_type == "visitor_count_in",
                          MetricSnapshot.period_start >= since,
                          MetricSnapshot.period_start < until)
                  .group_by("hour_bucket").all())
        best = max(((h, float(v or 0)) for h, v in rows if h is not None),
                   key=lambda kv: kv[1], default=None)
        if best and best[1] > 0:
            h = best[0]
            if h.tzinfo is None:
                h = h.replace(tzinfo=timezone.utc)
            start = h.astimezone(EAT)
            end = start + timedelta(hours=1)
            fmt = lambda d: d.strftime("%I:00 %p").lstrip("0")  # noqa: E731
            out["peak"] = {"window": f"{fmt(start)} – {fmt(end)}",
                           "visitors": int(best[1])}
    except Exception:
        log.error("status report: peak block failed:\n%s",
                  traceback.format_exc())
        out["degraded"] = True

    try:   # 4) operator feedback clicks
        confirmed = (db.query(func.count(Alert.id))
                       .filter(Alert.status == "confirmed",
                               Alert.acknowledged_at >= since,
                               Alert.acknowledged_at < until).scalar() or 0)
        dismissed = (db.query(func.count(Alert.id))
                       .filter(Alert.status == "dismissed",
                               Alert.acknowledged_at >= since,
                               Alert.acknowledged_at < until).scalar() or 0)
        out["feedback"] = {"confirmed": int(confirmed),
                           "dismissed": int(dismissed)}
    except Exception:
        log.error("status report: feedback block failed:\n%s",
                  traceback.format_exc())
        out["degraded"] = True
    return out


# ── section 5: live health in plain language ─────────────────────────


def _friendly_health(snap: dict) -> tuple[str, list[tuple[str, str]]]:
    """(headline, [(marker, sentence), ...]) — no jargon, no raw keys."""
    from app.utils.system_health import overall_status
    emoji, label = overall_status(snap)
    headline = {"Healthy": "All Systems Healthy ✅",
                "Warning": "Minor Warnings ⚠️",
                "Critical": "Attention Needed 🔴"}[label]

    lines: list[tuple[str, str]] = []
    conts = snap.get("containers") or []
    bad = [c["name"] for c in conts if not c.get("healthy")]
    if conts and not bad:
        lines.append(("✅", f"All {len(conts)} services running"))
    elif bad:
        lines.append(("🔴" if {"postgres", "redis"} & set(bad) else "⚠️",
                      f"{len(bad)} service(s) not responding: "
                      f"{', '.join(bad)} — IT should check the server"))

    cams = snap.get("cameras") or {}
    if cams.get("total"):
        if cams.get("offline", 0) == 0:
            lines.append(("✅", f"{cams['streaming']} cameras streaming"))
        else:
            lines.append(("⚠️",
                          f"{cams['streaming']} of {cams['total']} cameras "
                          f"streaming — {cams['offline']} offline"
                          + (f" ({', '.join(cams['offline_names'][:6])}"
                             + ("…" if len(cams['offline_names']) > 6 else "")
                             + ")" if cams.get("offline_names") else "")))

    sto = snap.get("storage") or {}
    if sto.get("percent_used") is not None:
        pct = sto["percent_used"]
        mark = "🔴" if pct > 90 else ("⚠️" if pct > 80 else "✅")
        lines.append((mark,
                      f"Storage: {pct:.0f}% used ({sto['used_gb']:.0f}GB / "
                      f"{sto['total_gb']:.0f}GB) — {sto['free_gb']:.0f}GB free"
                      + (" — please free up space soon" if pct > 80 else "")))

    mo = snap.get("model") or {}
    if mo.get("version"):
        m50 = f" (map50={mo['map50']:.3f})" if mo.get("map50") is not None else ""
        lines.append(("✅", f"Model {mo['version']} deployed{m50}"))
    else:
        lines.append(("⚠️", "No AI model marked as deployed"))

    det = (snap.get("detection") or {}).get("events_last_30min_by_type") or {}
    if det:
        top = ", ".join(f"{k} {v}" for k, v in
                        sorted(det.items(), key=lambda kv: -kv[1])[:3])
        lines.append(("✅", f"Detection pipeline active ({top})"))
    else:
        lines.append(("⚠️", "No detections in the last 30 minutes — "
                            "normal before opening, worth a look otherwise"))

    tr = snap.get("training") or {}
    if tr.get("jobs_queued", 0) > 5:
        lines.append(("⚠️", f"{tr['jobs_queued']} training jobs waiting — "
                            f"will resume automatically"))

    if snap.get("collection_errors"):
        lines.append(("⚠️", "Some stats unavailable this morning."))
    return headline, lines


# ── rendering ─────────────────────────────────────────────────────────

_TBL = ("border-collapse:collapse;font-size:14px;margin:6px 0 14px 0")
_TH = ("text-align:left;padding:6px 14px;border:1px solid #cbd5e1;"
       "background:#f1f5f9")
_TD = "padding:6px 14px;border:1px solid #cbd5e1"
_TDR = _TD + ";text-align:right"


def _esc(v) -> str:
    return _html.escape(str(v))


def _render_html(y: dict, headline: str,
                 health_lines: list[tuple[str, str]], date_str: str) -> str:
    parts = [f"""<html><body style="font-family:Arial,Helvetica,sans-serif;
color:#0f172a;max-width:640px">
<h2 style="margin-bottom:2px">VivoGuard Status Report</h2>
<p style="color:#64748b;margin-top:0">{_esc(date_str)}</p>"""]

    unavailable = ("<p>⚠️ Some stats unavailable this morning.</p>")

    # 1 — visitors
    parts.append(f"<h3>Total Visitors (Yesterday — {_esc(y['label'])})</h3>")
    if y["visitors"] is not None:
        parts.append(f"<div style='font-size:34px;font-weight:bold'>"
                     f"{y['visitors']['total']:,}</div>"
                     f"<table style='{_TBL}'>"
                     f"<tr><th style='{_TH}'>Store</th>"
                     f"<th style='{_TH}'>Visitors</th></tr>")
        for name, n in y["visitors"]["rows"]:
            parts.append(f"<tr><td style='{_TD}'>{_esc(name)}</td>"
                         f"<td style='{_TDR}'>{n:,}</td></tr>")
        parts.append("</table>")
    else:
        parts.append(unavailable)

    # 2 — alerts
    parts.append("<h3>Alerts Summary (Yesterday)</h3>")
    if y["alerts"] is not None:
        a = y["alerts"]
        parts.append(
            f"<table style='{_TBL}'>"
            f"<tr><th style='{_TH}'>Severity</th>"
            f"<th style='{_TH}'>Count</th></tr>"
            f"<tr><td style='{_TD}'>Critical / Urgent</td>"
            f"<td style='{_TDR}'>{a['URGENT']}</td></tr>"
            f"<tr><td style='{_TD}'>High / Needs Attention</td>"
            f"<td style='{_TDR}'>{a['ATTENTION']}</td></tr>"
            f"<tr><td style='{_TD}'>Medium / Info</td>"
            f"<td style='{_TDR}'>{a['INFO']}</td></tr></table>")
    else:
        parts.append(unavailable)

    # 3 — peak time
    parts.append("<h3>Peak Time (Yesterday)</h3>")
    if y["peak"]:
        parts.append(f"<p>Busiest hour chain-wide: "
                     f"<b>{_esc(y['peak']['window'])}</b> "
                     f"({y['peak']['visitors']:,} visitors)</p>")
    elif y["visitors"] is not None:
        parts.append("<p>No clear peak — very low traffic recorded.</p>")
    else:
        parts.append(unavailable)

    # 4 — AI training feedback
    parts.append("<h3>AI Training Feedback (Yesterday)</h3>")
    if y["feedback"] is not None:
        f = y["feedback"]
        parts.append(
            f"<p>True alerts confirmed: <b>{f['confirmed']}</b> — used as "
            f"positive training samples<br>"
            f"False alerts dismissed: <b>{f['dismissed']}</b> — used as "
            f"negative training samples</p>")
    else:
        parts.append(unavailable)

    # 5 — live system health
    parts.append(f"<h3>System Health — {_esc(headline)}</h3>")
    for mark, sentence in health_lines:
        parts.append(f"<p style='margin:3px 0'>{mark} {_esc(sentence)}</p>")

    parts.append("<p style='color:#94a3b8;font-size:12px;margin-top:18px'>"
                 "Automated daily report — VivoGuard AI</p></body></html>")
    return "".join(parts)


def _render_text(y: dict, headline: str,
                 health_lines: list[tuple[str, str]], date_str: str) -> str:
    L = [f"VivoGuard Status Report — {date_str}", ""]
    L.append(f"Total Visitors (Yesterday — {y['label']})")
    if y["visitors"] is not None:
        L.append(f"  {y['visitors']['total']:,} visitors")
        for name, n in y["visitors"]["rows"]:
            L.append(f"    {name}: {n:,}")
    else:
        L.append("  Some stats unavailable this morning.")
    L.append("")
    if y["alerts"] is not None:
        a = y["alerts"]
        L += ["Alerts (Yesterday):",
              f"  Critical/Urgent: {a['URGENT']}  "
              f"High/Needs Attention: {a['ATTENTION']}  "
              f"Medium/Info: {a['INFO']}"]
    if y["peak"]:
        L.append(f"Busiest hour (Yesterday): {y['peak']['window']} "
                 f"({y['peak']['visitors']:,} visitors)")
    if y["feedback"] is not None:
        L.append(f"Training feedback (Yesterday): "
                 f"{y['feedback']['confirmed']} confirmed / "
                 f"{y['feedback']['dismissed']} dismissed")
    L += ["", f"System Health — {headline}"]
    L += [f"  {m} {s}" for m, s in health_lines]
    return "\n".join(L)


# ── the task ──────────────────────────────────────────────────────────


@celery_app.task(name="system.daily_status_report", bind=True,
                 ignore_result=True, max_retries=_MAX_RETRIES)
def daily_status_report(self, force: bool = False) -> None:
    """5-min tick; sends once per day at/after 11:30 EAT. SMTP failure
    retries every 15 min (up to 4), bypassing the clock gate but never
    the sent marker. force=True (manual .delay(force=True)) skips the
    clock gate only."""
    now_eat = datetime.now(EAT)
    day_iso = now_eat.date().isoformat()
    is_retry = int(getattr(self.request, "retries", 0) or 0) > 0
    if not (force or is_retry):
        if not (now_eat.hour == _FIRE_HOUR
                and _FIRE_MINUTE <= now_eat.minute
                < _FIRE_MINUTE + _FIRE_WINDOW_MIN):
            return
    if not settings.smtp_host:
        log.warning("status report: SMTP not configured — skipping")
        return

    r = None
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True,
                            socket_timeout=3)
    except Exception as e:
        log.warning("status report: redis unavailable (%s) — sending "
                    "without dedupe", e)
    if r is not None:
        try:
            if r.get(_sent_key(day_iso)):
                log.info("status report: already sent today (%s) — skipping",
                         day_iso)
                return
            if not (force or is_retry) and not r.set(
                    _lock_key(day_iso), "1", nx=True, ex=1200):
                return   # another tick is already building/sending
        except Exception:
            pass

    try:
        from app.database import SessionLocal
        from app.utils.system_health import collect_system_health
        with SessionLocal() as db:
            y = _collect_yesterday(db)
            snap = collect_system_health(db)
        headline, health_lines = _friendly_health(snap)
        if y["degraded"]:
            health_lines.append(("⚠️", "Some stats unavailable this morning."))
        date_str = now_eat.strftime("%A, %d %B %Y")
        subject = f"VivoGuard Status Report — {day_iso}"

        msg = EmailMessage()
        msg["From"] = settings.smtp_from
        msg["To"] = ", ".join(sorted(SYSTEM_ADMIN_EMAILS))
        msg["Subject"] = subject
        msg.set_content(_render_text(y, headline, health_lines, date_str))
        msg.add_alternative(_render_html(y, headline, health_lines, date_str),
                            subtype="html")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                          timeout=30) as s:
            if settings.smtp_use_tls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    except Exception as e:
        attempt = int(getattr(self.request, "retries", 0) or 0) + 1
        log.error("status report: send failed (attempt %d/%d): %s",
                  attempt, _MAX_RETRIES + 1, e)
        raise self.retry(exc=e, countdown=_RETRY_COUNTDOWN_S)

    if r is not None:
        try:
            r.set(_sent_key(day_iso), "1", ex=_SENT_TTL_S)
        except Exception:
            pass
    log.info("status report sent to %d recipients (%s)",
             len(SYSTEM_ADMIN_EMAILS), day_iso)


# Back-compat alias: beat entries or operators calling the old task name
# keep working until every container restarts on the new schedule.
@celery_app.task(name="system.health_daily_report", ignore_result=True)
def system_health_daily_report(force: bool = False) -> None:
    daily_status_report.apply(kwargs={"force": force})
