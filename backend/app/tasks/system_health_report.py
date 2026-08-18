"""Daily system-health email — 08:00 EAT to the platform operators.

House pattern (same as reports.dispatch_due / the recorder): a 5-min
beat tick that checks the EAT wall clock, with a Redis SET-NX day
marker so restarts inside the window can't double-send. Recipients
come from app.utils.system_admins.SYSTEM_ADMIN_EMAILS — the same
single constant the /system-health endpoint guards with.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from app.config import settings
from app.tasks.celery_app import celery_app
from app.utils.system_admins import SYSTEM_ADMIN_EMAILS

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")
_FIRE_HOUR = 8            # 08:00 EAT
_FIRE_WINDOW_MIN = 15     # fire anywhere in 08:00-08:15 (beat tick is 5 min)
_DEDUPE_TTL_S = 20 * 60 * 60


def _dedupe_key(day_iso: str) -> str:
    return f"vg:syshealth:report:{day_iso}"


@celery_app.task(name="system.health_daily_report", ignore_result=True)
def system_health_daily_report(force: bool = False) -> None:
    """Every 5 min; sends once per day inside the 08:00-08:15 EAT
    window. `force=True` (manual .delay(force=True)) skips the clock
    gate but NOT the dedupe."""
    now_eat = datetime.now(EAT)
    if not force:
        if not (now_eat.hour == _FIRE_HOUR
                and now_eat.minute < _FIRE_WINDOW_MIN):
            return
    if not settings.smtp_host:
        log.warning("system-health report: SMTP not configured — skipping")
        return

    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True)
        if not r.set(_dedupe_key(now_eat.date().isoformat()), "1",
                     nx=True, ex=_DEDUPE_TTL_S):
            return   # already sent today
    except Exception as e:
        # Redis down: still send (worst case a duplicate) — a silent
        # missing report is worse than a doubled one.
        log.warning("system-health report: dedupe unavailable: %s", e)

    from app.database import SessionLocal
    from app.utils.system_health import collect_system_health, overall_status
    with SessionLocal() as db:
        snap = collect_system_health(db)
    emoji, label = overall_status(snap)

    date_str = now_eat.strftime("%A %d %B %Y")
    subject = f"VivoGuard System Health Report — {now_eat.date().isoformat()}"
    html = _render_html(snap, emoji, label, date_str)
    text = _render_text(snap, emoji, label, date_str)

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(sorted(SYSTEM_ADMIN_EMAILS))
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                          timeout=30) as s:
            if settings.smtp_use_tls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        log.info("system-health report sent to %d recipients (%s)",
                 len(SYSTEM_ADMIN_EMAILS), label)
    except Exception:
        log.exception("system-health report: SMTP send failed")


# ── rendering ────────────────────────────────────────────────────────


def _render_text(snap: dict, emoji: str, label: str, date_str: str) -> str:
    cams, sto = snap["cameras"], snap["storage"]
    tr, al, mo = snap["training"], snap["alerts"], snap["model"]
    lines = [
        f"VivoGuard System Health — {date_str}",
        f"Overall: {emoji} {label}",
        "",
        f"Storage: {sto['used_gb']}/{sto['total_gb']} GB "
        f"({sto['percent_used']}% used)"
        + ("  ⚠ OVER 80%" if (sto.get("percent_used") or 0) > 80 else ""),
        f"Cameras: {cams['streaming']}/{cams['total']} streaming, "
        f"{cams['offline']} offline",
    ]
    if cams["offline_names"]:
        lines.append("Offline: " + ", ".join(cams["offline_names"]))
    lines += [
        f"Model: {mo['version'] or 'none deployed'} "
        f"(mAP50 {mo['map50']}, P {mo['precision']}, R {mo['recall']})",
        f"Training: {tr['jobs_queued']} queued, {tr['jobs_running']} running, "
        f"{tr['jobs_completed_today']} completed today "
        f"(latest mAP50 {tr['latest_map50']})",
        f"Alerts today: {al['urgent_today']} urgent, "
        f"{al['pending_today']} pending, {al['resolved_today']} resolved",
        f"Detection events today: {snap['detection']['total_events_today']}",
    ]
    unhealthy = [c["name"] for c in snap["containers"] if not c["healthy"]]
    if unhealthy:
        lines.append("Unhealthy services: " + ", ".join(unhealthy))
    return "\n".join(lines)


def _render_html(snap: dict, emoji: str, label: str, date_str: str) -> str:
    cams, sto = snap["cameras"], snap["storage"]
    tr, al, mo, de = (snap["training"], snap["alerts"], snap["model"],
                      snap["detection"])
    pct = sto.get("percent_used") or 0
    bar_color = "#dc2626" if pct > 90 else ("#d97706" if pct > 80 else "#16a34a")
    storage_warn = ("<p style='color:#dc2626;font-weight:bold'>⚠ Storage above "
                    "80% — prune recordings or expand the volume.</p>"
                    if pct > 80 else "")

    def row(k: str, v) -> str:
        return (f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>{k}</td>"
                f"<td style='padding:4px 0'><b>{v}</b></td></tr>")

    containers_rows = "".join(
        f"<tr><td style='padding:3px 12px 3px 0'>{c['name']}</td>"
        f"<td style='padding:3px 12px 3px 0'>{'🟢' if c['healthy'] else '🔴'} "
        f"{c['status']}</td>"
        f"<td style='padding:3px 0;color:#64748b'>{c['uptime'] or ''}</td></tr>"
        for c in snap["containers"])
    offline_html = (
        "<p><b>Offline cameras:</b> " + ", ".join(cams["offline_names"]) + "</p>"
        if cams["offline_names"] else "")
    by_type = de["events_last_30min_by_type"]
    by_type_html = (", ".join(f"{k}: {v}" for k, v in
                              sorted(by_type.items(), key=lambda kv: -kv[1]))
                    or "none")

    return f"""\
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#0f172a;max-width:680px">
  <h2 style="margin-bottom:0">{emoji} VivoGuard System Health — {label}</h2>
  <p style="color:#64748b;margin-top:4px">{date_str} · generated {snap['generated_at']}</p>

  <h3>Storage</h3>
  <div style="background:#e2e8f0;border-radius:6px;height:18px;width:100%">
    <div style="background:{bar_color};height:18px;border-radius:6px;width:{min(pct, 100)}%"></div>
  </div>
  <p>{sto['used_gb']} / {sto['total_gb']} GB used ({pct}%) — {sto['free_gb']} GB free.
     Recordings: {sto['recordings_gb'] if sto['recordings_gb'] is not None else '?'} GB,
     alert clips: {sto['alert_clips_gb'] if sto['alert_clips_gb'] is not None else '?'} GB.</p>
  {storage_warn}

  <h3>Cameras</h3>
  <p>{cams['streaming']} / {cams['total']} streaming · {cams['offline']} offline ·
     {cams['ai_enabled_active']} AI-active</p>
  {offline_html}

  <h3>Services</h3>
  <table style="border-collapse:collapse;font-size:14px">{containers_rows}</table>

  <h3>Model</h3>
  <table style="border-collapse:collapse;font-size:14px">
    {row('Deployed', mo['version'] or 'none')}
    {row('mAP50', mo['map50'] if mo['map50'] is not None else '—')}
    {row('Precision', mo['precision'] if mo['precision'] is not None else '—')}
    {row('Recall', mo['recall'] if mo['recall'] is not None else '—')}
    {row('Since', (mo['deployed_since'] or '—')[:10])}
  </table>

  <h3>Training pipeline</h3>
  <p>{tr['jobs_queued']} queued · {tr['jobs_running']} running ·
     {tr['jobs_completed_today']} completed today ·
     latest mAP50 {tr['latest_map50'] if tr['latest_map50'] is not None else '—'}</p>

  <h3>Alerts (today)</h3>
  <p>🔴 {al['urgent_today']} urgent · ⏳ {al['pending_today']} pending ·
     ✅ {al['resolved_today']} resolved</p>

  <h3>Detection</h3>
  <p>{de['total_events_today']} events today · last 30 min: {by_type_html}</p>

  <p style="color:#94a3b8;font-size:12px">Automated report — VivoGuard AI ·
     full dashboard: /system-health (system admins only)</p>
</body></html>"""
