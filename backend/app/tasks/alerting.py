"""Sustained-condition alerting — celery beat tasks that escalate
queue length + camera offline events to WhatsApp.

These are deliberately SEPARATE from the per-frame DetectionEvent
pipeline because they only fire on conditions that persist over time:

  - queue_escalation_check     queue length > threshold sustained > N minutes
  - camera_health_check        camera offline > 5 minutes during business hours

Both write WhatsApp to settings.dashboard_alert_to and dedup via a
Redis marker so a single sustained incident produces one nudge.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta

import redis

from app.config import settings
from app.tasks.celery_app import celery_app
from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient

log = logging.getLogger(__name__)

# Sustained-queue thresholds. Operator-facing copy ("⚠️ Long queue …")
# uses the actual measured count + minutes so we don't lie when the
# threshold drifts.
QUEUE_COUNT_THRESHOLD   = 5     # > 5 people
QUEUE_DURATION_SECONDS  = 180   # for > 3 minutes
QUEUE_DEDUP_TTL_SECONDS = 600   # one WhatsApp per zone per 10 min

# Camera-health thresholds.
CAMERA_OFFLINE_THRESHOLD_SECONDS = 5 * 60      # > 5 min
CAMERA_HEALTH_DEDUP_TTL_SECONDS  = 30 * 60     # one nudge per camera / 30 min


def _redis():
    return redis.from_url(settings.redis_url, decode_responses=True)


def _dashboard_recipients() -> list[str]:
    """Resolve the ops escalation number from settings → twilio format.
    Returns [] when Twilio isn't configured so dev installs stay quiet."""
    raw = settings.dashboard_alert_to or ""
    out: list[str] = []
    for part in raw.split(","):
        norm = _format_whatsapp_recipient(part.strip())
        if norm:
            out.append(norm)
    return out


# ---- Queue escalation -------------------------------------------------

@celery_app.task(name="alerting.queue_escalation_check", ignore_result=True)
def queue_escalation_check() -> None:
    """Scan the latest queue_length metric snapshots for every camera.
    When count > threshold AND has been > threshold for > duration,
    fire one WhatsApp.

    Sustained-state tracking is in Redis:
      vg:queue_alert:start:{store_id}:{camera_id}:{zone_id} → epoch when high count first observed
      vg:queue_alert:sent:{store_id}:{camera_id}:{zone_id}  → set once WhatsApp went out (TTL = dedup)
    """
    from app.database import SessionLocal
    from app.models import Camera, MetricSnapshot, Store, Zone
    from sqlalchemy import desc

    r = _redis()
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=120)

    with SessionLocal() as db:
        # Latest queue_length snapshot per (camera, zone) in the last 2 min.
        rows = (db.query(MetricSnapshot)
                  .filter(MetricSnapshot.metric_type == "queue_length",
                          MetricSnapshot.period_start >= cutoff)
                  .order_by(MetricSnapshot.period_start.desc())
                  .all())
        latest: dict[tuple[int, int], MetricSnapshot] = {}
        for m in rows:
            key = (m.camera_id or 0, m.zone_id or 0)
            if key not in latest:
                latest[key] = m

        for (cam_id, zone_id), snap in latest.items():
            count = int(snap.value or 0)
            store_id = snap.store_id or 0
            start_key = f"vg:queue_alert:start:{store_id}:{cam_id}:{zone_id}"
            sent_key  = f"vg:queue_alert:sent:{store_id}:{cam_id}:{zone_id}"

            if count <= QUEUE_COUNT_THRESHOLD:
                # Reset the run when the queue drains.
                r.delete(start_key)
                continue

            started = r.get(start_key)
            if not started:
                r.set(start_key, str(int(now_ts)), ex=2 * QUEUE_DURATION_SECONDS)
                continue

            duration = now_ts - int(started)
            if duration < QUEUE_DURATION_SECONDS:
                continue

            if r.get(sent_key):
                continue

            store = db.get(Store, store_id) if store_id else None
            zone  = db.get(Zone,  zone_id)  if zone_id  else None
            cam   = db.get(Camera, cam_id)  if cam_id   else None
            store_name = (store.name if store else None) or "Unknown store"
            zone_name  = (zone.name  if zone  else None) or "checkout"
            cam_name   = (cam.name   if cam   else None) or f"camera {cam_id}"
            minutes = max(1, int(duration / 60))

            body = (f"⚠️ Long queue at {store_name} ({zone_name}) — "
                    f"{count} people waiting {minutes} minutes "
                    f"[{cam_name}]")
            recipients = _dashboard_recipients()
            sent = _send_whatsapp(recipients, body)
            log.info("queue escalation: %s (%d people %dm) → %d WhatsApp sent",
                     store_name, count, minutes, sent)
            r.set(sent_key, "1", ex=QUEUE_DEDUP_TTL_SECONDS)


# ---- Camera health ----------------------------------------------------

@celery_app.task(name="alerting.camera_health_check", ignore_result=True)
def camera_health_check() -> None:
    """Walk active cameras during their store's business hours. Any
    camera whose `last_seen_at` is older than 5 minutes earns a
    WhatsApp nudge (deduped per-camera per-30-minutes)."""
    from app.database import SessionLocal
    from app.models import Camera, Store
    from app.utils.business_hours import is_store_open

    r = _redis()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=CAMERA_OFFLINE_THRESHOLD_SECONDS)

    with SessionLocal() as db:
        cameras = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
        for cam in cameras:
            if cam.store_id is None:
                continue
            store = db.get(Store, cam.store_id)
            if not store or not is_store_open(store, now):
                continue   # we only nudge during operating hours

            last_seen = cam.last_seen_at
            if last_seen is not None and last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)

            if last_seen is not None and last_seen >= cutoff:
                # Online — clear any latched outage marker.
                r.delete(f"vg:cam_offline:start:{cam.id}")
                r.delete(f"vg:cam_offline:sent:{cam.id}")
                continue

            start_key = f"vg:cam_offline:start:{cam.id}"
            sent_key  = f"vg:cam_offline:sent:{cam.id}"

            if not r.get(start_key):
                # First detection — record start, hold off on alerting.
                r.set(start_key, str(int(now.timestamp())),
                      ex=24 * 3600)
                continue

            if r.get(sent_key):
                continue

            # Compute uptime over the last 24h: fraction of the last
            # 24h where the camera was reporting frames. Approximated
            # using the last_seen field — if last_seen < 24h ago, the
            # camera was up until that moment, so:
            #   uptime = (now - 24h until last_seen) / 24h
            window_start = now - timedelta(hours=24)
            if last_seen is None or last_seen < window_start:
                uptime_pct = 0
            else:
                up_seconds = (last_seen - window_start).total_seconds()
                uptime_pct = max(0, min(100, int(up_seconds / (24 * 3600) * 100)))

            last_seen_str = last_seen.astimezone(timezone.utc).strftime("%H:%M UTC") \
                if last_seen else "never"
            body = (f"⚠️ Camera offline: {cam.name} at {store.name}. "
                    f"Last seen {last_seen_str}. 24h uptime {uptime_pct}%.")
            recipients = _dashboard_recipients()
            sent = _send_whatsapp(recipients, body)
            log.info("camera health: %s (%s) offline → %d WhatsApp sent",
                     cam.name, store.name, sent)
            r.set(sent_key, "1", ex=CAMERA_HEALTH_DEDUP_TTL_SECONDS)
