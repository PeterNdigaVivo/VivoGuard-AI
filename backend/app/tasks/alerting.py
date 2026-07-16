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
import json
import logging
import os
from datetime import datetime, timezone, timedelta

import redis

from app.config import settings
from app.tasks.celery_app import celery_app
from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
from app.utils.business_hours import is_store_open

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


# ---- Business-hours gate for operational alerts -----------------------
#
# Retail operational alerts (long checkout, counter unstaffed, uniform
# violation) are meaningless before a store opens — staff arrive early,
# tills sit empty overnight. This gate suppresses them outside operating
# hours so we stop firing e.g. the 07:37 EAT "checkout taking too long"
# nudge before the doors open. Safety alerts (weapon, intrusion, fire)
# are NOT routed through this gate — they must fire 24/7.
DEFAULT_OPEN_HOUR_EAT  = 8    # 08:00 — used only when a store has no
DEFAULT_CLOSE_HOUR_EAT = 21   # 21:00   business_hours_json configured.


def _within_operating_hours(store) -> bool:
    """True if the store is currently within operating hours, in its own
    timezone (Africa/Nairobi / EAT by default).

    - Store WITH configured business_hours_json → exact per-day windows
      via is_store_open (respects split shifts and closed days).
    - Store with no configured hours, or a storeless legacy camera →
      default EAT window DEFAULT_OPEN_HOUR_EAT..DEFAULT_CLOSE_HOUR_EAT.
    - Timezone unresolvable → returns True (do not suppress) so a config
      error never silences alerts silently.
    """
    if store is not None and getattr(store, "business_hours_json", None):
        return is_store_open(store)
    tz_name = getattr(store, "timezone", None) or "Africa/Nairobi"
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        return True
    return DEFAULT_OPEN_HOUR_EAT <= now_local.hour < DEFAULT_CLOSE_HOUR_EAT


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


# ---- Inference pipeline health (URGENT) -----------------------------
#
# The supervisor (inference.supervise_all) writes vg:inference:health
# every 30s. If that key ages past 10 min the inference pipeline is
# stalled — the supervisor itself is down, or every worker that runs
# the `inference` queue is down. Either way the camera fleet is going
# uncovered RIGHT NOW and ops must know.

INFERENCE_STALL_THRESHOLD_S      = 10 * 60   # 10 min
INFERENCE_STALL_DEDUP_TTL_S      = 30 * 60   # one URGENT per 30 min


@celery_app.task(name="alerting.inference_pipeline_health_check",
                  ignore_result=True)
def inference_pipeline_health_check() -> None:
    """Fire URGENT when the supervisor health breadcrumb ages past
    INFERENCE_STALL_THRESHOLD_S. Per-30-min dedup. Early warning
    before a full outage."""
    from zoneinfo import ZoneInfo
    r = _redis()
    if r is None:
        return
    now_ts = datetime.now(timezone.utc).timestamp()

    try:
        raw = r.get("vg:inference:health")
    except Exception as e:
        log.exception("inference_pipeline_health_check: redis read failed: %s", e)
        return

    last_run_ts: float | None = None
    cameras_total: int = 0
    if raw:
        try:
            payload = json.loads(raw)
            last_run_ts = float(payload.get("last_run_ts") or 0) or None
            cameras_total = int(payload.get("cameras_total") or 0)
        except Exception:
            last_run_ts = None

    age = (now_ts - last_run_ts) if last_run_ts else None
    healthy = age is not None and age <= INFERENCE_STALL_THRESHOLD_S
    if healthy:
        # Clear dedup when health is back so the NEXT outage can fire.
        try: r.delete("vg:inference_pipeline_alert:sent")
        except Exception: pass
        return

    sent_key = "vg:inference_pipeline_alert:sent"
    if r.get(sent_key):
        return

    from app.database import SessionLocal
    from app.models import Camera
    # The health breadcrumb may be missing (supervisor never ran since
    # deploy) — fall back to the live count of ai_enabled cameras so
    # the body still says something useful.
    if not cameras_total:
        try:
            with SessionLocal() as db:
                cameras_total = (db.query(Camera)
                                  .filter(Camera.ai_enabled == True)        # noqa: E712
                                  .count())
        except Exception:
            cameras_total = 0

    when = (datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
                  .astimezone(ZoneInfo("Africa/Nairobi"))
                  .strftime("%H:%M EAT")
            if last_run_ts else "unknown")
    age_min = int((age or 0) / 60)
    body = (f"Inference pipeline stalled — {cameras_total} cameras "
            f"not being monitored. Last successful supervisor tick: "
            f"{when} ({age_min} min ago).")
    extra = {
        "priority":   "high",
        "rule":       "inference_pipeline_stalled",
        "store_id":   None,
        "title":      "🚨 AI inference pipeline stalled",
        "message":    body,
        "what_to_do": [
            "Check that the worker-inference container is running",
            "Check Redis connectivity from the worker host",
            "Restart the worker-inference container if both look fine",
        ],
        "cameras_total":     cameras_total,
        "last_run_age_sec":  int(age) if age else None,
    }
    # No camera_id → use the first ai_enabled camera as an FK anchor
    # (matches the shop_not_opened URGENT pattern; the alert is fleet-
    # scoped but the schema needs a camera_id).
    anchor_cam_id = None
    try:
        from app.database import SessionLocal as _SL
        from app.models import Camera as _Cam
        with _SL() as db:
            row = (db.query(_Cam.id)
                     .filter(_Cam.ai_enabled == True)                      # noqa: E712
                     .order_by(_Cam.id.asc()).first())
            anchor_cam_id = int(row[0]) if row else None
            if anchor_cam_id:
                _create_info_alert(
                    db, camera_id=anchor_cam_id, zone_id=None, store_id=None,
                    detection_type="system_health",
                    cls="inference_pipeline_stalled", extra=extra,
                )
                db.commit()
    except Exception as e:
        log.exception("inference_pipeline_health_check: alert write failed: %s", e)
        return

    try:
        recipients = _dashboard_recipients()
        if recipients:
            _send_whatsapp(recipients, f"🚨 {body}")
    except Exception:
        pass

    r.set(sent_key, "1", ex=INFERENCE_STALL_DEDUP_TTL_S)
    log.warning("inference_pipeline_stalled: cameras=%d age_min=%d",
                cameras_total, age_min)


# ---- Checkout dwell (long-session ATTENTION alert) -------------------
#
# The CheckoutDwellDetector publishes a per-session Redis key when a
# customer enters a counter zone:
#   vg:checkout_open:{cam}:{zone}:{track}  →  JSON {entry_ts, store_id, …}
#
# This task scans those keys every 60s. When ANY live session's age
# exceeds settings.checkout_alert_minutes, it fires ONE ATTENTION
# alert per STORE and stamps a 30-minute dedup key so the same
# prolonged transaction doesn't repeat-fire — and so further long
# checkouts at ANY till in that store during the window are suppressed.
# Previously deduped per (store, camera, zone), which let every till
# fire independently and flooded the feed (257 alerts/day). Coarsening
# to per-store collapses that to one checkout nudge per store / 30 min.
#
# Dedup key:
#   vg:checkout_alert:sent:{store_id}          (TTL = 30 min)
#   vg:checkout_alert:sent:cam:{cam_id}        (storeless legacy cameras)

CHECKOUT_ALERT_DEDUP_TTL_SECONDS = 30 * 60


@celery_app.task(name="alerting.checkout_long_session_check",
                  ignore_result=True)
def checkout_long_session_check() -> None:
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Camera, Store, Zone

    threshold_minutes = int(getattr(settings, "checkout_alert_minutes", 8))
    threshold_seconds = max(60, threshold_minutes * 60)
    r = _redis()
    if r is None:
        return
    now_ts = datetime.now(timezone.utc).timestamp()

    # Walk every open-session key. `scan_iter` is non-blocking and
    # OK at the volume we're dealing with (one key per active
    # checkout, store-wide — typically a handful).
    candidates: list[dict] = []
    try:
        for key in r.scan_iter(match="vg:checkout_open:*", count=200):
            raw = r.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            entry_ts = float(payload.get("entry_ts") or 0.0)
            age = now_ts - entry_ts
            # Per-session floor stamped by CheckoutDwellDetector for
            # medium-confidence-staff sessions — they get more grace
            # than the global threshold so a staff member serving a
            # tricky customer doesn't trip the 8-min default.
            session_floor = int(payload.get("min_alert_seconds") or 0)
            effective_threshold = max(threshold_seconds, session_floor)
            if age < effective_threshold:
                continue
            candidates.append({
                "key": (key.decode() if isinstance(key, bytes) else key),
                "age": age,
                "store_id":  payload.get("store_id"),
                "camera_id": int(payload.get("camera_id") or 0),
                "zone_id":   int(payload.get("zone_id")   or 0),
                "track_id":  payload.get("track_id"),
            })
    except Exception as e:
        log.exception("checkout_long_session_check: scan failed: %s", e)
        return

    if not candidates:
        return

    with SessionLocal() as db:
        for c in candidates:
            cam_id   = c["camera_id"]
            zone_id  = c["zone_id"]
            store_id = c["store_id"]
            # One checkout-dwell alert per STORE per 30 min (see
            # CHECKOUT_ALERT_DEDUP_TTL_SECONDS). Storeless legacy cameras
            # have no store to group by, so they fall back to per-camera
            # dedup rather than collapsing into one shared bucket.
            sent_key = (f"vg:checkout_alert:sent:{store_id}"
                        if store_id else
                        f"vg:checkout_alert:sent:cam:{cam_id}")
            if r.get(sent_key):
                continue

            store = db.get(Store,  store_id) if store_id else None
            # Don't alert outside store hours — a "checkout taking too
            # long" nudge before opening (e.g. 07:37 EAT) is noise. Skip
            # WITHOUT setting the dedup marker so it can still fire once
            # the store is open and the session is genuinely long.
            if not _within_operating_hours(store):
                continue
            zone  = db.get(Zone,   zone_id)  if zone_id  else None
            cam   = db.get(Camera, cam_id)   if cam_id   else None
            store_name = (store.name if store else None) or "Unknown store"
            zone_name  = (zone.name  if zone  else None) or "checkout"
            cam_name   = (cam.name   if cam   else None) or f"camera {cam_id}"
            minutes = max(1, int(c["age"] / 60))

            body = (f"A customer has been at the {zone_name} for "
                    f"{minutes} minutes at {store_name}. "
                    f"This may indicate a transaction issue or staff "
                    f"needing support.")
            extra = {
                "priority":      "attention",
                "rule":          "checkout_dwell_long",
                "store_id":      store_id,
                "store_name":    store_name,
                "camera_name":   cam_name,
                "zone_name":     zone_name,
                "dwell_minutes": minutes,
                "dwell_seconds": round(c["age"], 1),
                "threshold_minutes": threshold_minutes,
                "title":         f"Long Checkout — {store_name}",
                "message":       body,
                "what_to_do": [
                    "Check the live camera at the counter",
                    "Send a staff member to assist if needed",
                    "Mark resolved once the customer has been served",
                ],
            }
            ev_rec = _create_info_alert(
                db, camera_id=cam_id or None, zone_id=zone_id or None,
                store_id=store_id, detection_type="checkout_dwell",
                cls="checkout_dwell_long", extra=extra,
            )
            db.commit()
            r.set(sent_key, "1", ex=CHECKOUT_ALERT_DEDUP_TTL_SECONDS)
            log.info("checkout_dwell_long: store=%s cam=%s zone=%s "
                     "duration=%dm", store_name, cam_id, zone_id, minutes)

            # Promote any pending timeline snapshots into the alert
            # folder and stamp Alert.snapshot_paths. Also write
            # `alert_id` back into the open-session Redis payload so
            # the detector's session-close hook can append late
            # frames to the same alert row.
            try:
                from app.ai.detectors.checkout_snapshots import promote_to_alert
                from app.ai.detectors.retail_checkout import (
                    REDIS_OPEN_KEY_FMT as _CK_KEY,
                    REDIS_OPEN_TTL_SECONDS as _CK_TTL,
                )
                from app.models import Alert as _Alert
                track_id_int = int(c.get("track_id") or 0) if c.get("track_id") is not None else 0
                # Look up the Alert row by event_id — _create_info_alert
                # returned the DetectionEvent, not the Alert.
                alert_row = (db.query(_Alert)
                               .filter(_Alert.event_id == ev_rec.id)
                               .first())
                if alert_row and track_id_int:
                    paths = promote_to_alert(
                        camera_id=cam_id, track_id=track_id_int,
                        zone_id=zone_id, alert_id=alert_row.id,
                    )
                    if paths:
                        alert_row.snapshot_paths = paths
                        db.commit()
                    # Stamp alert_id into the open-session key so the
                    # detector's _finalise_snapshots can find it later.
                    open_key = _CK_KEY.format(
                        cam=cam_id, zone=zone_id, track=track_id_int)
                    raw = r.get(open_key)
                    if raw:
                        payload = json.loads(raw)
                        payload["alert_id"] = int(alert_row.id)
                        r.set(open_key, json.dumps(payload), ex=_CK_TTL)
            except Exception:
                log.exception("checkout_dwell_long: snapshot promote "
                              "failed event=%s", getattr(ev_rec, "id", None))

            # VLM scene analysis — async on the alerts queue (we're
            # already on it, but .delay keeps it non-blocking + lets
            # the task re-check vlm_enabled / dedup). checkout_dwell is
            # in the default vlm_alert_types.
            try:
                from app.config import settings as _vlm_settings
                if (getattr(_vlm_settings, "vlm_enabled", False)
                        and "checkout_dwell" in set(
                            getattr(_vlm_settings, "vlm_alert_types", []) or [])):
                    from app.models import Alert as _Alert2
                    a2 = (db.query(_Alert2)
                            .filter(_Alert2.event_id == ev_rec.id).first())
                    if a2 is not None:
                        from app.tasks.vlm_tasks import analyse_alert_scene
                        analyse_alert_scene.delay(a2.id)
            except Exception:
                log.exception("checkout_dwell_long: vlm enqueue failed "
                              "event=%s", getattr(ev_rec, "id", None))


# ---- Checkout snapshot retention (24h prune) -------------------------

@celery_app.task(name="alerting.prune_checkout_snapshots",
                  ignore_result=True)
def prune_checkout_snapshots() -> None:
    """Delete checkout-dwell timeline snapshot files + null
    Alert.snapshot_paths for alerts older than 24h. Per the Q4
    spec: retention is measured from Alert.created_at. Idempotent —
    re-running over already-pruned rows is a no-op."""
    from app.database import SessionLocal
    from app.models import Alert
    from app.ai.detectors.checkout_snapshots import delete_alert_folder
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with SessionLocal() as db:
        stale = (db.query(Alert)
                   .filter(Alert.snapshot_paths.isnot(None))
                   .filter(Alert.created_at < cutoff)
                   .all())
        n_files = 0
        for a in stale:
            n_files += delete_alert_folder(int(a.id))
            a.snapshot_paths = None
        if stale:
            db.commit()
        log.info("prune_checkout_snapshots: alerts=%d files=%d",
                 len(stale), n_files)


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


# ---- Uniform-violation manager notification ---------------------------

UNIFORM_DEDUP_TTL_SECONDS = 30 * 60   # one WhatsApp per store per 30 min


@celery_app.task(name="alerting.uniform_violation_check", ignore_result=True)
def uniform_violation_check() -> None:
    """Notify the store manager (falling back to the ops number) when a
    uniform-compliance violation alert fired in the last ~2 minutes.
    Runs off the inference hot path so the WhatsApp round-trip never
    stalls a camera loop. Deduped per store per 30 min."""
    from app.database import SessionLocal
    from app.models import Alert, Camera, DetectionEvent, Store

    r = _redis()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=130)

    with SessionLocal() as db:
        rows = (db.query(DetectionEvent, Camera)
                  .join(Alert, Alert.event_id == DetectionEvent.id)
                  .join(Camera, Camera.id == DetectionEvent.camera_id)
                  .filter(DetectionEvent.detection_type == "uniform_compliance",
                          DetectionEvent.timestamp >= cutoff)
                  .all())
        # One notification per store, even if several cameras fired.
        by_store: dict[int, tuple] = {}
        for ev, cam in rows:
            if cam.store_id is not None and cam.store_id not in by_store:
                by_store[cam.store_id] = (ev, cam)

        for store_id, (ev, cam) in by_store.items():
            sent_key = f"vg:uniform_alert:sent:{store_id}"
            if r.get(sent_key):
                continue
            store = db.get(Store, store_id)
            # Staff aren't expected before opening, so a uniform-
            # compliance nudge outside store hours is noise. Skip without
            # setting the dedup marker.
            if not _within_operating_hours(store):
                continue
            store_name = store.name if store else f"store {store_id}"
            rule = (ev.extra or {}).get("rule", "uniform_violation")
            if rule == "no_lanyard":
                body = (f"🔵 Staff missing name tag at {store_name} "
                        f"[{cam.name}]")
            else:
                body = (f"⚠️ Staff uniform violation at {store_name} "
                        f"[{cam.name}]")
                if (ev.extra or {}).get("repeated_today"):
                    body = (f"🔴 Repeated uniform violations at {store_name} "
                            f"today [{cam.name}]")

            # Prefer the store manager's number; fall back to ops.
            recipients = []
            mgr = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
            if mgr:
                recipients.append(mgr)
            recipients.extend(_dashboard_recipients())
            recipients = list(dict.fromkeys(recipients))  # dedup, keep order

            sent = _send_whatsapp(recipients, body)
            log.info("uniform violation: %s [%s] → %d WhatsApp sent",
                     store_name, cam.name, sent)
            r.set(sent_key, "1", ex=UNIFORM_DEDUP_TTL_SECONDS)


# ---- Sales Floor Intelligence (15-min plain-English heartbeat) -------
#
# Fires every 15 minutes during 09:00-21:00 EAT for each store that
# has at least one zone tagged 'aisle' (the canonical sales-floor tag)
# or the legacy 'dwell' alias. Builds an INFO alert in the database
# summarising the last 15 minutes of customer browsing + staff
# coverage, so operators get a continuous plain-English read on what's
# happening AND a heartbeat that the AI system is still running.
#
# Dedupe: one alert per store per 15-min bucket, via Redis with a 30
# min TTL — the bucket key already prevents collisions, the TTL is
# just a safety net.

SFI_WINDOW_MIN          = 15
SFI_LOW_ENGAGEMENT_S    = 60.0     # avg browse time < 1 min → ATTENTION
SFI_GOOD_ENGAGEMENT_S   = 120.0    # avg browse time ≥ 2 min → "good"
SFI_UNATTENDED_MIN_CUST = 1        # ≥ this many customers + no staff
                                    # for the whole window → unattended
SFI_HEARTBEAT_FRESH_MIN = 20       # camera "active" if it produced a
                                    # metric_snapshot or detection_event
                                    # in the last N minutes — the
                                    # streamer never writes Camera.status,
                                    # so DB-flag reads always said 0.


def _fmt_browse_time(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    m, s = divmod(s, 60)
    if m and s:
        return f"{m} min {s} sec"
    if m:
        return f"{m} min"
    return f"{s} sec"


def _store_eat_now(store):
    from zoneinfo import ZoneInfo
    tz_name = getattr(store, "timezone", None) or "Africa/Nairobi"
    try:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Nairobi"))


def _sfi_metric_summary(db, store, cam_ids: list[int], zone_id_to_name: dict[int, str],
                         window_start_utc, now_utc):
    """Aggregate the last-15-min metric_snapshots into a plain payload
    the alert body + classifier consume."""
    from app.models import MetricSnapshot
    from sqlalchemy import func

    # browse_time_seconds → average across zones (recorded with avg aggregator
    # by AisleDwellDetector). dwell_count is per-zone "customers in zone now"
    # written at "last" aggregator — we take the MAX across the window per
    # zone as the high-water mark, then sum zones for a store total.
    # occupancy → "people in frame right now" written by OccupancyMetricsDetector;
    # MAX over the window per camera + sum across cameras gives a peak-people
    # figure that includes BOTH staff and customers (useful for the
    # quiet-period wording: "1 person detected" vs "no customers").
    rows = (db.query(MetricSnapshot.zone_id,
                     MetricSnapshot.camera_id,
                     MetricSnapshot.metric_type,
                     MetricSnapshot.value,
                     MetricSnapshot.period_start)
              .filter(MetricSnapshot.camera_id.in_(cam_ids),
                      MetricSnapshot.period_start >= window_start_utc,
                      MetricSnapshot.period_start <  now_utc,
                      MetricSnapshot.metric_type.in_((
                          "browse_time_seconds", "dwell_seconds",
                          "dwell_count", "staff_present", "occupancy")))
              .all())

    # Per-zone collectors.
    browse_avg_by_zone: dict[int, list[float]] = {}
    dwell_count_peak:   dict[int, float]       = {}
    staff_pres_by_zone: dict[int, list[float]] = {}
    occupancy_peak_by_cam: dict[int, float]    = {}

    for zid, cam_id, mtype, value, _ts in rows:
        v = float(value or 0.0)
        if mtype == "occupancy":
            if cam_id is not None:
                occupancy_peak_by_cam[cam_id] = max(
                    occupancy_peak_by_cam.get(cam_id, 0.0), v)
            continue
        if zid is None:
            continue
        if zid not in zone_id_to_name:
            continue        # not an aisle zone
        if mtype in ("browse_time_seconds", "dwell_seconds"):
            browse_avg_by_zone.setdefault(zid, []).append(v)
        elif mtype == "dwell_count":
            dwell_count_peak[zid] = max(dwell_count_peak.get(zid, 0.0), v)
        elif mtype == "staff_present":
            staff_pres_by_zone.setdefault(zid, []).append(v)

    # Per-zone average + winner.
    per_zone: dict[int, dict] = {}
    for zid, name in zone_id_to_name.items():
        avgs = browse_avg_by_zone.get(zid) or []
        zone_avg = sum(avgs) / len(avgs) if avgs else 0.0
        per_zone[zid] = {
            "name":  name,
            "avg":   zone_avg,
            "peak":  int(dwell_count_peak.get(zid, 0)),
        }
    winner = max(per_zone.values(),
                 key=lambda z: z["avg"], default=None) if per_zone else None

    # Store-wide rollups.
    all_avgs = [v for vs in browse_avg_by_zone.values() for v in vs]
    avg_browse_s = sum(all_avgs) / len(all_avgs) if all_avgs else 0.0
    total_customers = int(sum(dwell_count_peak.values()))
    all_staff = [v for vs in staff_pres_by_zone.values() for v in vs]
    staff_coverage = (sum(all_staff) / len(all_staff)) if all_staff else 0.0
    # Peak SIMULTANEOUS people seen across the store during the window
    # — sum of per-camera occupancy peaks. Slightly over-counts when a
    # person is on two cameras at once; that's acceptable for the
    # quiet-period wording ("X people detected" vs the customer-only
    # `total_customers`).
    people_peak = int(sum(occupancy_peak_by_cam.values()))

    return {
        "avg_browse_s":   avg_browse_s,
        "total_customers": total_customers,
        "people_peak":    people_peak,
        "staff_coverage": staff_coverage,
        "per_zone":       per_zone,
        "winner":         winner,
    }


def _sfi_classify(summary: dict, hb: dict | None = None) -> tuple[str, str]:
    """Map summary → (rule, priority). Rules drive the alert title +
    body + severity rendering in app.api.alerts.

    Detection-health gate (Vivo DIGO RD MSA bug, June 2026): when
    the heartbeat says NO cameras are streaming or NO inference has
    run yet for this store, the zero-customer reading is a SYSTEM
    issue, not a quiet period. Returning 'detection_offline' here
    short-circuits the cheerful "normal for this time of day" body
    and routes the alert through the system-issue copy + ATTENTION
    severity instead.
    """
    if hb is not None:
        cams_active = int(hb.get("cameras_active") or 0)
        last_inf    = hb.get("last_inference_min")
        if cams_active == 0 or last_inf is None:
            return "detection_offline", "warning"

    avg = summary["avg_browse_s"]
    customers = summary["total_customers"]
    coverage = summary["staff_coverage"]

    if customers == 0:
        return "quiet_period", "info"
    if customers >= SFI_UNATTENDED_MIN_CUST and coverage <= 0.05:
        return "unattended_floor", "warning"
    if avg > 0 and avg < SFI_LOW_ENGAGEMENT_S:
        return "low_engagement", "warning"
    if avg >= SFI_GOOD_ENGAGEMENT_S:
        return "good_engagement", "info"
    return "sales_floor_update", "info"   # neutral/baseline


def _sfi_heartbeat(db, store, cam_ids: list[int], now_utc) -> dict:
    """Camera-health snippet — cameras that have actually produced
    inference output in the last SFI_HEARTBEAT_FRESH_MIN minutes.

    "Active" used to read `Camera.last_seen_at`, but the streamer
    never updates that column, so the figure was always 0/N even on
    healthy stores. The real signal is "did this camera write a
    metric_snapshot or a detection_event recently?" — we now query
    both tables for distinct camera ids in the window and take the
    union as the active set."""
    from sqlalchemy import func
    from app.models import DetectionEvent, MetricSnapshot
    if not cam_ids:
        return {"cameras_active": 0, "cameras_total": 0,
                "last_inference_min": None}

    fresh_cutoff = now_utc - timedelta(minutes=SFI_HEARTBEAT_FRESH_MIN)

    ms_cams = {
        cid for (cid,) in db.query(MetricSnapshot.camera_id)
                            .filter(MetricSnapshot.camera_id.in_(cam_ids),
                                    MetricSnapshot.period_start >= fresh_cutoff)
                            .distinct().all()
        if cid is not None
    }
    de_cams = {
        cid for (cid,) in db.query(DetectionEvent.camera_id)
                            .filter(DetectionEvent.camera_id.in_(cam_ids),
                                    DetectionEvent.timestamp >= fresh_cutoff)
                            .distinct().all()
        if cid is not None
    }
    active_cams = ms_cams | de_cams

    # Freshest write across either table for the store.
    ms_max = (db.query(func.max(MetricSnapshot.period_start))
                .filter(MetricSnapshot.camera_id.in_(cam_ids),
                        MetricSnapshot.period_start >= fresh_cutoff)
                .scalar())
    de_max = (db.query(func.max(DetectionEvent.timestamp))
                .filter(DetectionEvent.camera_id.in_(cam_ids),
                        DetectionEvent.timestamp >= fresh_cutoff)
                .scalar())
    most_recent = None
    for ts in (ms_max, de_max):
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if most_recent is None or ts > most_recent:
            most_recent = ts
    last_inference_min = None
    if most_recent is not None:
        delta = (now_utc - most_recent).total_seconds()
        last_inference_min = max(0, int(round(delta / 60)))

    return {
        "cameras_active":     len(active_cams),
        "cameras_total":      len(cam_ids),
        "last_inference_min": last_inference_min,
    }


def _people_count_line(customers: int, people_peak: int) -> str:
    """One brief line summarising who was on camera in the window.
    `people_peak` (from `occupancy`) includes both staff and
    customers; staff is the residual after subtracting the customer
    count. Falls back to a total-people line when there's no useful
    split (no customers and no staff)."""
    customers = max(0, int(customers))
    people_peak = max(0, int(people_peak))
    staff_est = max(0, people_peak - customers)
    cust_word = "customer" if customers == 1 else "customers"
    staff_word = "staff member" if staff_est == 1 else "staff"
    if people_peak == 0 and customers == 0:
        return "👥 No people detected in store"
    if staff_est == 0:
        return f"👥 {customers} {cust_word} in store"
    if customers == 0:
        return (f"👥 {staff_est} {staff_word} detected on sales floor "
                "(no customers)")
    return (f"👥 {customers} {cust_word} in store · "
            f"{staff_est} {staff_word} detected on sales floor")


def _sfi_compose_body(store_name: str, summary: dict, rule: str,
                       hb: dict) -> str:
    """Plain-English alert body. Every rule renders the same one-line
    people-count summary up front; the rest of the body is the
    rule-specific copy + the AI System Status footer."""
    avg_text = _fmt_browse_time(summary["avg_browse_s"])
    customers = summary["total_customers"]
    people_peak = int(summary.get("people_peak") or 0)
    coverage_pct = int(round(summary["staff_coverage"] * 100))
    winner = summary["winner"]
    cams_active = int(hb.get("cameras_active") or 0)
    cams_total  = int(hb.get("cameras_total")  or 0)
    last_inf    = hb.get("last_inference_min")
    people_line = _people_count_line(customers, people_peak)

    # Detection-offline override: no person/dwell metrics landed for
    # this store in the window. Could be a quiet store with cameras
    # that didn't produce a metric write, a streamer warm-up gap, or
    # an actual camera issue — the worker doesn't know which, so we
    # keep the copy strictly factual and let the AI System Status
    # line below carry the numbers.
    if rule == "detection_offline":
        last = (f"{last_inf} minutes ago"
                if last_inf is not None else "not yet")
        return "\n".join([
            "No detection data recorded for this store in the last 15 minutes.",
            "",
            f"📷 Cameras active: {cams_active}/{cams_total}",
            f"⚡ Last inference: {last}",
        ])

    lines: list[str] = ["In the last 15 minutes:",
                        people_line,
                        f"👥 {customers} customers browsed your product aisles",
                        f"⏱️ Average browse time: {avg_text}"]
    if winner and winner["avg"] > 0:
        lines.append(
            f"🏆 Most popular area: {winner['name']} "
            f"({_fmt_browse_time(winner['avg'])} avg)")
    lines.append(f"👔 Staff coverage: {coverage_pct}% of the time")

    if rule == "good_engagement":
        lines += ["",
                  "What this means: Customers are spending good time "
                  "browsing your products."]
        if winner:
            lines.append(f"{winner['name']} is getting the most attention.")
    elif rule == "low_engagement":
        lines += ["",
                  "What this means: Customers are spending less than 1 min "
                  "browsing. Consider improving displays or moving popular "
                  "items to more visible locations."]
    elif rule == "unattended_floor":
        lines += ["",
                  "What this means: Customers are browsing without staff in "
                  "sight. Please send a staff member to the sales floor."]
    elif rule == "quiet_period":
        # Brief one-line summary — the people-count line above already
        # carries the actual numbers. No multi-tier rewrite needed.
        lines = [people_line,
                 "Quiet period — light store activity in the last 15 minutes."]

    last = (f"{last_inf} minutes ago"
            if last_inf is not None else "not yet")
    lines += ["",
              "🤖 AI System Status: Running normally",
              f"📷 Cameras active: {cams_active}/{cams_total}",
              f"⚡ Last inference: {last}"]
    return "\n".join(lines)


def _create_info_alert(db, *, camera_id: int, zone_id: int | None,
                        store_id: int | None, detection_type: str,
                        cls: str, extra: dict,
                        capture_snapshot: bool = True):
    """Write a DetectionEvent + Alert row directly (no detector frame
    needed). Used by the sales-floor heartbeat.

    When `capture_snapshot=True` we ALSO save the anchor camera's
    most recent cached JPEG to disk and pin it to event.thumbnail_path
    so the alerts UI shows the frame AT THE TIME OF DETECTION rather
    than a live re-fetch. The /alerts/{id}/snapshot endpoint already
    prefers thumbnail_path over the live frame buffer.
    """
    from app.models import Alert, DetectionEvent
    thumb_path: str | None = None
    if capture_snapshot and camera_id is not None:
        thumb_path = _save_alert_thumbnail(camera_id, detection_type)
    rec = DetectionEvent(
        camera_id=camera_id,
        zone_id=zone_id,
        detection_type=detection_type,
        confidence=1.0,
        bbox_json=[0, 0, 1, 1],
        extra=extra,
        thumbnail_path=thumb_path,
    )
    db.add(rec); db.flush()
    db.add(Alert(event_id=rec.id, status="new"))
    return rec


def _save_alert_thumbnail(camera_id: int, detection_type: str) -> str | None:
    """Pull the latest cached JPEG for `camera_id` from the streamer
    and save it under data/recordings/snapshots/YYYY-MM-DD/. Returns
    the on-disk path or None on any failure — a missing snapshot must
    never block alert creation."""
    try:
        from pathlib import Path
        from datetime import datetime as _dt
        from app.config import settings
        from app.stream.frame_buffer import FrameBuffer
        jpeg = FrameBuffer().latest_jpeg(int(camera_id))
        if not jpeg:
            return None
        root = Path(settings.recordings_dir) / "snapshots" / _dt.now().strftime("%Y-%m-%d")
        root.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime("%H%M%S_%f")
        path = root / f"{detection_type}_cam{camera_id}_{ts}.jpg"
        path.write_bytes(jpeg)
        return str(path)
    except Exception as e:
        log.warning("alert thumbnail save failed for camera %s: %s",
                    camera_id, e)
        return None


@celery_app.task(name="alerting.sales_floor_insights_check", ignore_result=True)
def sales_floor_insights_check() -> None:
    """Every 15 min during 09:00-21:00 EAT (per-store), create one
    INFO alert per store summarising the last 15 minutes of customer
    browsing + staff coverage on the sales floor.

    Skips stores that have no zone tagged 'aisle' / 'dwell'.
    Skips stores that aren't currently inside business hours (in
    their local timezone — Africa/Nairobi default).
    Per-store + per-15-min-bucket Redis dedupe so two beat ticks in
    the same bucket can't double-fire.

    Logging policy: every branch logs at INFO level so the worker
    log alone explains why an expected alert didn't fire (no zones,
    closed hours, dedup, exception). Hard failures inside the
    per-store body are caught and logged with full stack traces."""
    from app.database import SessionLocal
    from app.models import Store

    try:
        r = _redis()
    except Exception as e:
        log.exception("sales_floor_insights: redis unavailable, aborting: %s", e)
        return

    now_utc = datetime.now(timezone.utc)
    window_start_utc = now_utc - timedelta(minutes=SFI_WINDOW_MIN)
    # Belt-and-braces debug-flag resolution. settings.sales_floor_debug
    # is the canonical source (pydantic-settings reads SALES_FLOOR_DEBUG
    # from .env), but if the worker container was started without
    # picking up the env var we also consult os.environ directly so a
    # restart isn't required when ops flips the flag.
    settings_debug = bool(getattr(settings, "sales_floor_debug", False))
    env_debug = (os.environ.get("SALES_FLOOR_DEBUG", "")
                 .strip().lower() in ("1", "true", "yes", "on"))
    debug = settings_debug or env_debug
    log.info("sales_floor_insights: debug_resolved=%s (settings=%s env=%s)",
             debug, settings_debug, env_debug)
    if debug:
        log.warning("sales_floor_insights: DEBUG MODE — bypassing dedupe + "
                    "closed-hours + aisle-zone gates")
    created = 0
    skipped = 0
    failed = 0

    try:
        with SessionLocal() as db:
            stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
            log.info("sales_floor_insights: checking %d active stores", len(stores))
            for store in stores:
                try:
                    outcome = _maybe_emit_sfi_for_store(
                        db, r, store, now_utc, window_start_utc, debug=debug)
                    if outcome == "created":
                        created += 1
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    log.exception("sales_floor_insights: store=%s (%s) FAILED: %s",
                                  store.id, store.name, e)
                    try: db.rollback()
                    except Exception: pass
            log.info("sales_floor_insights: done — created=%d skipped=%d failed=%d",
                     created, skipped, failed)
    except Exception as e:
        log.exception("sales_floor_insights: top-level failure: %s", e)


def _maybe_emit_sfi_for_store(db, r, store, now_utc, window_start_utc,
                               *, debug: bool = False) -> str:
    """Returns one of:
      'created'       — alert row written
      'closed_hours'  — outside trading hours (bypassed by debug)
      'dedup'         — bucket already fired (bypassed by debug)
      'no_cameras'    — store has no cameras attached
      'no_aisle_zone' — no zone tagged 'aisle' or 'dwell'
                       (bypassed by debug — alert still fires)
    """
    from app.models import Camera, Zone

    # Local time gate — only run inside trading hours.
    local = _store_eat_now(store)
    if not (9 <= local.hour < 21) and not debug:
        log.info("sales_floor_insights: store=%s (%s) skipped — closed hours "
                 "(local=%s)", store.id, store.name, local.strftime("%H:%M"))
        return "closed_hours"

    # Per-15-min-bucket dedupe. Buckets are :00 :15 :30 :45 EAT —
    # exactly 4 per hour per store. floor(min/15)*15 → {0,15,30,45}.
    bucket = local.replace(minute=(local.minute // 15) * 15, second=0,
                           microsecond=0).strftime("%Y%m%dT%H%M")
    sent_key = f"vg:sfi:sent:{store.id}:{bucket}"
    # Debug-mode short-circuit happens BEFORE the Redis read so a
    # broken cache or a stale key from a non-debug tick can't silence
    # a debug tick. Spec rule: "In debug mode — always proceed,
    # never check dedup key".
    if debug:
        pass    # fire regardless
    else:
        if r.get(sent_key):
            log.info("sales_floor_insights: store=%s (%s) skipped — already "
                     "fired for bucket %s", store.id, store.name, bucket)
            return "dedup"

    cams = db.query(Camera).filter(Camera.store_id == store.id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        log.info("sales_floor_insights: store=%s (%s) skipped — no cameras "
                 "attached", store.id, store.name)
        return "no_cameras"

    zones = (db.query(Zone)
               .filter(Zone.camera_id.in_(cam_ids)).all())
    aisle_zones = [z for z in zones
                   if {"aisle", "dwell"} & set(z.detection_types_json or [])]
    if not aisle_zones:
        if not debug:
            log.info("sales_floor_insights: store=%s (%s) skipped — no "
                     "aisle/dwell zones configured (cameras=%d, zones=%d). "
                     "Draw an aisle zone on at least one camera to start "
                     "receiving updates.",
                     store.id, store.name, len(cam_ids), len(zones))
            return "no_aisle_zone"
        # Debug mode: fire anyway. No aisle zone → use first camera,
        # no zone FK. Empty zone_id_to_name means _sfi_metric_summary
        # returns the zero-customer baseline, which classifies as
        # 'quiet_period' — exactly the heartbeat we want for ops.
        log.warning("sales_floor_insights: store=%s (%s) DEBUG firing without "
                    "aisle zones (cameras=%d)", store.id, store.name, len(cam_ids))
        zone_id_to_name: dict[int, str] = {}
        anchor_zone = None
        anchor_cam_id = cams[0].id
    else:
        zone_id_to_name = {z.id: (z.name or f"Zone {z.id}") for z in aisle_zones}
        anchor_zone = aisle_zones[0]
        anchor_cam_id = anchor_zone.camera_id

    summary = _sfi_metric_summary(db, store, cam_ids, zone_id_to_name,
                                  window_start_utc, now_utc)
    log.info("sales_floor_insights: store=%s (%s) zones=%d browse_records=%d "
             "customers=%d avg_browse=%.1fs coverage=%.2f",
             store.id, store.name, len(aisle_zones),
             sum(1 for z in summary["per_zone"].values() if z["avg"] > 0),
             summary["total_customers"], summary["avg_browse_s"],
             summary["staff_coverage"])

    # Build the heartbeat FIRST so the classifier can downgrade a
    # zero-customer reading to 'detection_offline' when cameras are
    # not actually streaming. Otherwise we mislabel a system issue
    # as a "quiet period" (Vivo DIGO RD MSA bug, June 2026).
    hb = _sfi_heartbeat(db, store, cam_ids, now_utc)
    rule, priority = _sfi_classify(summary, hb)
    body = _sfi_compose_body(store.name or f"Store {store.id}",
                             summary, rule, hb)

    extra = {
        "priority":            priority,
        "rule":                rule,
        "store_id":            store.id,
        "store_name":          store.name,
        "window_minutes":      SFI_WINDOW_MIN,
        "bucket_eat":          bucket,
        "avg_browse_seconds":  round(summary["avg_browse_s"], 1),
        "total_customers":     summary["total_customers"],
        "people_peak":         int(summary.get("people_peak") or 0),
        "staff_coverage":      round(summary["staff_coverage"], 3),
        "top_zone_name":       (summary["winner"] or {}).get("name"),
        "top_zone_avg_s":      round((summary["winner"] or {}).get("avg", 0.0), 1),
        "cameras_active":      hb["cameras_active"],
        "cameras_total":       hb["cameras_total"],
        "last_inference_min":  hb["last_inference_min"],
        "message":             body,
        "eat_time":            local.strftime("%H:%M"),
    }
    _create_info_alert(db, camera_id=anchor_cam_id,
                       zone_id=(anchor_zone.id if anchor_zone else None),
                       store_id=store.id,
                       detection_type="sales_floor_insight",
                       cls=rule, extra=extra)
    db.commit()
    # Only mark the bucket dedupe in NORMAL mode — in debug we want
    # every 15-min tick to fire regardless of what already ran.
    if not debug:
        r.set(sent_key, "1", ex=60 * 60)
    log.info("sales_floor_insights: store=%s (%s) ALERT CREATED rule=%s "
             "customers=%s avg_browse=%.1fs coverage=%.2f bucket=%s%s",
             store.id, store.name, rule, summary["total_customers"],
             summary["avg_browse_s"], summary["staff_coverage"], bucket,
             " (debug)" if debug else "")
    return "created"


# ---- 18:00 Daily Sales Floor WhatsApp summary ------------------------

SFI_DAILY_HOUR = 18                  # 18:00 store-local trigger


@celery_app.task(name="alerting.sales_floor_daily_summary", ignore_result=True)
def sales_floor_daily_summary() -> None:
    """Per-store 18:00 EAT WhatsApp summary of today's sales-floor
    activity. 5-min beat tick + per-store-per-day Redis dedup matches
    the briefings pattern."""
    from app.database import SessionLocal
    from app.models import Store

    r = _redis()
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for store in stores:
            try:
                local = _store_eat_now(store)
                if local.hour < SFI_DAILY_HOUR:
                    continue
                today = local.date().isoformat()
                key = f"vg:sfi:daily:{store.id}:date"
                if r.get(key) == today:
                    continue
                _sfi_send_daily_for_store(db, store, local)
                r.set(key, today, ex=2 * 24 * 3600)
            except Exception as e:
                log.exception("sales-floor daily failed for store %s: %s",
                              store.id, e)


def _sfi_send_daily_for_store(db, store, local_now) -> None:
    from app.models import Camera, MetricSnapshot, Zone
    from sqlalchemy import func
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(getattr(store, "timezone", None) or "Africa/Nairobi")
    today_local_00 = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_utc = today_local_00.astimezone(timezone.utc)
    window_end_utc   = local_now.astimezone(timezone.utc)

    cams = db.query(Camera).filter(Camera.store_id == store.id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return
    zones = db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()
    aisle_zones = [z for z in zones
                   if {"aisle", "dwell"} & set(z.detection_types_json or [])]
    if not aisle_zones:
        return
    zone_id_to_name = {z.id: (z.name or f"Zone {z.id}") for z in aisle_zones}

    # Pull today's metric snapshots in one go.
    rows = (db.query(MetricSnapshot.zone_id, MetricSnapshot.metric_type,
                     MetricSnapshot.value, MetricSnapshot.period_start)
              .filter(MetricSnapshot.camera_id.in_(cam_ids),
                      MetricSnapshot.period_start >= window_start_utc,
                      MetricSnapshot.period_start <  window_end_utc,
                      MetricSnapshot.metric_type.in_((
                          "browse_time_seconds", "dwell_seconds",
                          "dwell_count", "staff_present")))
              .all())

    by_zone_browse: dict[int, list[float]] = {}
    by_zone_count:  dict[int, float]       = {}
    by_hour_count:  dict[int, float]       = {}
    staff_samples: list[float] = []

    for zid, mtype, value, ts in rows:
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local_hr = ts.astimezone(tz).hour if ts else 0
        v = float(value or 0.0)
        if zid is None or zid not in zone_id_to_name:
            if mtype == "staff_present":
                staff_samples.append(v)
            continue
        if mtype in ("browse_time_seconds", "dwell_seconds"):
            by_zone_browse.setdefault(zid, []).append(v)
        elif mtype == "dwell_count":
            by_zone_count[zid] = by_zone_count.get(zid, 0.0) + v
            by_hour_count[local_hr] = by_hour_count.get(local_hr, 0.0) + v
        elif mtype == "staff_present":
            staff_samples.append(v)

    all_avgs = [v for vs in by_zone_browse.values() for v in vs]
    avg_browse = sum(all_avgs) / len(all_avgs) if all_avgs else 0.0
    total_customers = int(sum(by_zone_count.values()))
    staff_coverage = (sum(staff_samples) / len(staff_samples)) if staff_samples else 0.0

    # Most popular zone — highest avg browse.
    winner_zid = max(by_zone_browse, key=lambda z: sum(by_zone_browse[z]) /
                                                    len(by_zone_browse[z]),
                     default=None)
    winner_name = zone_id_to_name.get(winner_zid, "—") if winner_zid else "—"

    # Peak + quiet hours.
    if by_hour_count:
        peak_hour  = max(by_hour_count, key=by_hour_count.get)
        # Only consider hours within trading and that actually have data.
        quiet_hour = min(by_hour_count, key=by_hour_count.get)
    else:
        peak_hour = quiet_hour = None

    def _hour_band(h: int | None) -> str:
        if h is None:
            return "—"
        return f"{h:02d}:00 - {(h + 1) % 24:02d}:00"

    body = (
        f"📊 Sales Floor Report — {store.name or 'Store ' + str(store.id)} "
        f"— Today\n\n"
        f"Most popular product area: {winner_name}\n"
        f"Avg customer browse time: {_fmt_browse_time(avg_browse)}\n"
        f"Total customers who browsed: {total_customers}\n"
        f"Staff coverage of sales floor: {int(round(staff_coverage * 100))}%\n\n"
        f"🏆 Peak browsing time: {_hour_band(peak_hour)}\n"
        f"⚠️ Quiet period: {_hour_band(quiet_hour)}\n"
    )
    if winner_name and winner_name != "—":
        body += (f"\nTip: Consider moving popular items from {winner_name} "
                 f"to the main floor during quiet periods.")

    recipients: list[str] = []
    mgr = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
    if mgr:
        recipients.append(mgr)
    recipients.extend(_dashboard_recipients())
    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        return
    sent = _send_whatsapp(recipients, body)
    log.info("sales-floor daily: store=%s customers=%s → %d WhatsApp sent",
             store.id, total_customers, sent)


# ---- "Store Not Opened" URGENT (line-crossing path) ------------------
#
# 5-min beat tick. For every store whose local EAT clock has crossed
# the not-opened cutoff (default 09:30) and where NO entry/exit camera
# has fired the daily "open" line-crossing alert, emit one URGENT
# "Store Not Opened" alert. Per-store-per-day Redis dedupe.

NOT_OPENED_DEDUP_TTL = 24 * 3600    # one URGENT per store per day


@celery_app.task(name="alerting.shop_not_opened_check", ignore_result=True)
def shop_not_opened_check() -> None:
    from app.database import SessionLocal
    from app.models import Store
    from app.ai.detectors.shop_state import _read_cfg

    r = _redis()
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for store in stores:
            try:
                _shop_not_opened_for_store(db, r, store, _read_cfg)
            except Exception as e:
                log.exception("shop_not_opened: store=%s failed: %s",
                              store.id, e)


def _entrance_cam_ids_for_store(db, store_id: int) -> list[int]:
    """Return the sorted list of camera ids for the store that should
    be treated as the canonical "is the store open?" sensors today.

    Priority — **glass_door zones win** when any exist for the store
    (their 0.65-conf + 2-frame-persistence filter eliminates the
    reflection flickers that plagued plain entry_exit lines). The
    bare `entry_exit` cameras are used only when no glass_door zone
    is configured.

    Empty list = the store has no drawn entrance line of any kind.
    Every shop-open signal in this file (line crossing, person-event
    sliding window, occupancy-metric fallback) must skip such stores
    rather than fall back to all cameras: a cleaner detected in the
    stockroom at 06:00 is not "store opened"."""
    from app.models import Camera, Zone
    cam_id_rows = db.query(Camera.id).filter(Camera.store_id == store_id).all()
    cam_ids = [c for (c,) in cam_id_rows]
    if not cam_ids:
        return []
    line_zones = [
        z for z in db.query(Zone).filter(Zone.camera_id.in_(cam_ids)).all()
        if z.shape == "line"
        and "entry_exit" in (z.detection_types_json or [])
    ]
    glass_cams = sorted({
        z.camera_id for z in line_zones
        if "glass_door" in (z.detection_types_json or [])
    })
    if glass_cams:
        return glass_cams
    return sorted({z.camera_id for z in line_zones})


def _shop_not_opened_for_store(db, r, store, read_cfg) -> None:
    """Single-store check called from the beat task."""
    from app.models import Camera
    from app.ai.detectors import shop_state as _shop_state

    cams = db.query(Camera).filter(Camera.store_id == store.id).all()
    if not cams:
        return
    cam_ids = [c.id for c in cams]
    # Only entrance cameras matter — those with an entry_exit line.
    entrance_cam_ids = _entrance_cam_ids_for_store(db, store.id)
    if not entrance_cam_ids:
        return       # no entry/exit line → out of scope for this rule

    # Use the cutoff from the FIRST entry_exit zone's detector config
    # (or the default if no overrides). Stays consistent with the
    # opening-alert thresholds the EntryExitDetector reads from
    # the same config block.
    cfg = read_cfg(None)
    if not cfg["not_open_cutoff_t"]:
        return

    local = _store_eat_now(store)
    if local.time() < cfg["not_open_cutoff_t"]:
        return       # cutoff hasn't passed yet

    day_iso = local.date().isoformat()
    # Already opened — by an entrance crossing OR by the new
    # occupancy-inference task. The store-level marker is the
    # single source of truth across both paths.
    if _shop_state.store_opened_today(store.id, day_iso) is not None:
        # The crossing/inference path already detected the open. Still
        # run the retro-labeller in case a previous deploy fired a
        # stale shop_not_opened URGENT this morning before any of the
        # newer signals landed.
        _retro_label_false_positives(db, store, cam_ids, cfg, local, day_iso)
        return
    # Belt + braces — also honour the per-camera open marker the
    # crossing path writes (it's set in the same transaction as the
    # store-level marker, but reading both keeps the URGENT path
    # safe if a previous deploy missed the store-level write).
    for cam_id in entrance_cam_ids:
        if r.get(f"vg:shop_open_close:fired:{cam_id}:{day_iso}:open"):
            _retro_label_false_positives(db, store, cam_ids, cfg, local, day_iso)
            return   # already opened — nothing to do

    # Per-store-per-day dedupe for the URGENT itself.
    sent_key = f"vg:shop_open_close:not_opened:{store.id}:{day_iso}"

    # Third evidence tier — occupancy metrics. Crossings (high
    # confidence) and person-detection sliding-window (medium)
    # already had their say. Before crying wolf, check
    # metric_snapshots: if ANY camera in the store recorded
    # occupancy>0 between earliest_open and the cutoff, customers
    # were physically present so the store was open — the detection
    # pipeline just missed the door. Fire an INFO instead of the
    # URGENT and claim the opened-today marker.
    fb = _occupancy_fallback_for_store(db, _shop_state, store,
                                        entrance_cam_ids, cam_ids,
                                        cfg, local, day_iso)
    if fb is not None:
        # Fallback fired — also block any future URGENT for today.
        r.set(sent_key, "1", ex=NOT_OPENED_DEDUP_TTL)
        return

    if r.get(sent_key):
        return

    cutoff_hhmm = cfg["not_open_cutoff_t"].strftime("%H:%M")
    body = (f"{store.name or 'Store ' + str(store.id)} has not opened as of "
            f"{cutoff_hhmm}. No staff detected at the entrance.")
    extra = {
        "priority":          "high",
        "rule":              "shop_not_opened",
        "store_id":          store.id,
        "store_name":        store.name,
        "message":           body,
        "eat_time":          local.strftime("%H:%M"),
        "not_open_cutoff":   cutoff_hhmm,
        "signal":            "no_inward_crossing_by_cutoff",
    }
    # Anchor the event on the first entrance camera so the snapshot
    # path has a real FK to attach to.
    _create_info_alert(db, camera_id=entrance_cam_ids[0],
                       zone_id=None, store_id=store.id,
                       detection_type="shop_open_close",
                       cls="shop_not_opened", extra=extra)
    db.commit()
    r.set(sent_key, "1", ex=NOT_OPENED_DEDUP_TTL)

    # WhatsApp the ops list too — this is a manager-actionable URGENT.
    recipients: list[str] = []
    mgr = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
    if mgr:
        recipients.append(mgr)
    recipients.extend(_dashboard_recipients())
    recipients = list(dict.fromkeys(recipients))
    if recipients:
        _send_whatsapp(recipients, f"🚨 {body}")
    log.warning("shop_not_opened: store=%s (%s) past cutoff %s — URGENT fired",
                store.id, store.name, cutoff_hhmm)


# ---- Occupancy-metric fallback + false-positive retro-labeller -------
#
# Today (and historically) a number of stores fired the
# `shop_not_opened` URGENT at 09:30 EAT even though staff and
# customers were clearly on camera — the entrance-crossing path
# missed (bad line direction, dropped frames, no line drawn) AND
# person-detection events were too sparse for the sliding-window
# inference rule. The metric_snapshots table, however, was still
# being written: occupancy>0 samples for those cameras prove the
# store was open. Treat that as a third (lowest-confidence)
# evidence tier and use it for two things:
#
#   1. Suppress the URGENT — fire an INFO `shop_opened_via_occupancy`
#      and claim the opened-today marker.
#   2. Retroactively label any `shop_not_opened` DetectionEvent rows
#      from earlier today with {training_label: "false_positive",
#      evidence: "occupancy_present"} so we have a labelled dataset
#      to retrain the opening pipeline against.

OCCUPANCY_FALLBACK_METRICS = ("occupancy", "queue_length", "passersby")


def _morning_window_utc(cfg, local_now):
    """Returns (start_utc, end_utc) for today's "morning open window"
    in the store's local clock — i.e. earliest_open → not_open_cutoff
    in EAT, converted to UTC for the SQL filter."""
    from datetime import time as _time
    earliest_t = cfg.get("earliest_open_t") or _time(7, 0)
    cutoff_t   = cfg["not_open_cutoff_t"]
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = midnight.replace(hour=earliest_t.hour, minute=earliest_t.minute)
    end_local   = midnight.replace(hour=cutoff_t.hour,   minute=cutoff_t.minute)
    return (start_local.astimezone(timezone.utc),
            end_local.astimezone(timezone.utc))


def _occupancy_samples_in_window(db, cam_ids, start_utc, end_utc):
    """Returns rows of (period_start, value, camera_id, metric_type)
    for any occupancy/queue/passersby snapshot >0 for any of the
    given cameras in the window, ASCENDING by period_start."""
    from sqlalchemy import asc
    from app.models import MetricSnapshot
    if not cam_ids:
        return []
    return (db.query(MetricSnapshot.period_start,
                     MetricSnapshot.value,
                     MetricSnapshot.camera_id,
                     MetricSnapshot.metric_type)
              .filter(MetricSnapshot.camera_id.in_(cam_ids),
                      MetricSnapshot.metric_type.in_(OCCUPANCY_FALLBACK_METRICS),
                      MetricSnapshot.value > 0,
                      MetricSnapshot.period_start >= start_utc,
                      MetricSnapshot.period_start <= end_utc)
              .order_by(asc(MetricSnapshot.period_start))
              .limit(500)
              .all())


def _occupancy_fallback_for_store(db, shop_state, store, entrance_cam_ids,
                                   cam_ids, cfg, local_now, day_iso):
    """If metric_snapshots prove the store was open this morning,
    claim the opened marker, fire an INFO alert, retro-label any
    already-created shop_not_opened false positives, and return the
    earliest occupancy timestamp (EAT). Returns None when no
    occupancy evidence is found — the caller then proceeds to fire
    the URGENT as normal.

    Only metric_snapshots from ENTRANCE cameras count as evidence —
    a stockroom or back-counter camera recording occupancy at 06:00
    must not be treated as a store-open signal. The full `cam_ids`
    list is still accepted (for the retro-labeller, which walks
    every shop_open_close DetectionEvent for the store regardless
    of source camera).
    """
    from zoneinfo import ZoneInfo
    eat = ZoneInfo("Africa/Nairobi")

    if not entrance_cam_ids:
        return None     # no drawn entrance line → no inference possible

    start_utc, end_utc = _morning_window_utc(cfg, local_now)
    samples = _occupancy_samples_in_window(db, entrance_cam_ids,
                                             start_utc, end_utc)
    if not samples:
        return None

    earliest_period = samples[0][0]
    if earliest_period.tzinfo is None:
        earliest_period = earliest_period.replace(tzinfo=timezone.utc)
    opened_at_eat = earliest_period.astimezone(eat)
    peak_value = max(float(s[1]) for s in samples)

    # OPENING-WINDOW GATE (on the inferred OPEN TIME, not the current
    # clock). This function is the not-opened safety net and is called
    # by _shop_not_opened_for_store AT / AFTER the cutoff, so we must
    # NOT gate it on `now` — that would disable the guard and fire
    # false "Store Not Opened" URGENTs. Instead we require the inferred
    # opened_at to fall inside the opening window [earliest_open,
    # cutoff]. `_morning_window_utc` already bounds the sample query to
    # that window, so this is a belt-and-braces guard that keeps the
    # alert semantically "store opened during the opening window".
    earliest_t = cfg.get("earliest_open_t")
    cutoff_t   = cfg.get("not_open_cutoff_t")
    if earliest_t is not None and cutoff_t is not None:
        ot = opened_at_eat.time()
        if not (earliest_t <= ot <= cutoff_t):
            return None

    # Find a camera that contributed (preferring an entrance cam if
    # one of them recorded occupancy).
    entrance_set = set(entrance_cam_ids)
    contributing_cams = list(dict.fromkeys(int(s[2]) for s in samples))
    anchor_cam_id = next((c for c in contributing_cams if c in entrance_set),
                         contributing_cams[0] if contributing_cams
                         else entrance_cam_ids[0])

    # Atomic claim — losing the race is fine (some other path beat us).
    shop_state.mark_store_opened(
        store.id, day_iso,
        opened_at=opened_at_eat,
        method="occupancy_metric",
        confidence="low",
        camera_id=anchor_cam_id,
    )

    body = (f"{store.name or 'Store ' + str(store.id)} opened — "
            f"detected via occupancy data. Customers/staff were "
            f"recorded on camera at "
            f"{opened_at_eat.strftime('%H:%M')} but the entrance "
            f"line crossing was not captured.")
    extra = {
        "priority":          "info",
        "rule":              "shop_opened_via_occupancy",
        "store_id":          store.id,
        "store_name":        store.name,
        "message":           body,
        "method":            "occupancy_metric",
        "confidence":        "low",
        "opened_at_eat":     opened_at_eat.strftime("%H:%M"),
        "opened_at_iso":     opened_at_eat.isoformat(),
        "signal":            "occupancy_snapshot_present",
        "occupancy_samples": len(samples),
        "occupancy_peak":    peak_value,
        "evidence_cameras":  contributing_cams[:10],
    }
    _create_info_alert(
        db, camera_id=anchor_cam_id, zone_id=None, store_id=store.id,
        detection_type="shop_open_close", cls="shop_opened_via_occupancy",
        extra=extra,
    )

    # Backfill labels on any shop_not_opened rows from earlier today
    # for this store — they're confirmed false positives.
    _retro_label_false_positives(db, store, cam_ids, cfg, local_now,
                                  day_iso,
                                  evidence_sample_count=len(samples),
                                  evidence_peak=peak_value)
    db.commit()
    log.info("shop_opened_via_occupancy: store=%s opened_at=%s "
             "samples=%d peak=%.1f", store.id,
             opened_at_eat.isoformat(), len(samples), peak_value)
    return opened_at_eat


def _retro_label_false_positives(db, store, cam_ids, cfg, local_now,
                                  day_iso, *,
                                  evidence_sample_count: int | None = None,
                                  evidence_peak: float | None = None) -> None:
    """Marks today's shop_not_opened DetectionEvent rows for `store`
    as confirmed false positives in their `extra` JSON. Idempotent —
    rows that already carry a training_label are left alone."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import DetectionEvent

    midnight_local = local_now.replace(hour=0, minute=0,
                                        second=0, microsecond=0)
    day_start_utc = midnight_local.astimezone(timezone.utc)
    day_end_utc   = (midnight_local + timedelta(days=1)).astimezone(timezone.utc)

    rows = (db.query(DetectionEvent)
              .filter(DetectionEvent.camera_id.in_(cam_ids),
                      DetectionEvent.detection_type == "shop_open_close",
                      DetectionEvent.timestamp >= day_start_utc,
                      DetectionEvent.timestamp <  day_end_utc)
              .all())
    touched = 0
    for ev in rows:
        ex = dict(ev.extra or {})
        if ex.get("rule") != "shop_not_opened":
            continue
        if ex.get("training_label") == "false_positive":
            continue       # already labelled — idempotent
        ex["training_label"] = "false_positive"
        ex["evidence"]       = "occupancy_present"
        ex["labelled_at"]    = local_now.isoformat()
        if evidence_sample_count is not None:
            ex["evidence_sample_count"] = evidence_sample_count
        if evidence_peak is not None:
            ex["evidence_peak_occupancy"] = evidence_peak
        ev.extra = ex
        flag_modified(ev, "extra")
        touched += 1
    if touched:
        log.info("retro-labelled %d shop_not_opened false-positives "
                 "for store=%s (%s)", touched, store.id, day_iso)


# ---- Daily open/close summary (22:00 EAT) ----------------------------

SHOP_DAILY_HOUR = 22       # 22:00 store-local trigger


@celery_app.task(name="alerting.shop_daily_summary_check", ignore_result=True)
def shop_daily_summary_check() -> None:
    """5-min dispatcher. Per-store-per-day at 22:00 EAT, builds the
    open / close summary from today's shop_open_close DetectionEvent
    rows and creates one INFO alert + WhatsApp.

    "Vivo Yaya opened at 08:58 AM, closed at 21:05 PM
     Open for 12 hours 7 minutes today"
    """
    from app.database import SessionLocal
    from app.models import Store

    r = _redis()
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for store in stores:
            try:
                local = _store_eat_now(store)
                if local.hour < SHOP_DAILY_HOUR:
                    continue
                day_iso = local.date().isoformat()
                key = f"vg:shop_daily_summary:{store.id}:{day_iso}"
                if r.get(key):
                    continue
                _shop_daily_summary_for_store(db, store, local)
                r.set(key, "1", ex=2 * 24 * 3600)
            except Exception as e:
                log.exception("shop_daily_summary: store=%s failed: %s",
                              store.id, e)


def _shop_daily_summary_for_store(db, store, local_now) -> None:
    from zoneinfo import ZoneInfo
    from sqlalchemy import asc
    from app.models import Camera, DetectionEvent

    tz = ZoneInfo(getattr(store, "timezone", None) or "Africa/Nairobi")
    today_local_00 = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_utc = today_local_00.astimezone(timezone.utc)
    window_end_utc   = local_now.astimezone(timezone.utc)

    cams = db.query(Camera).filter(Camera.store_id == store.id).all()
    cam_ids = [c.id for c in cams]
    if not cam_ids:
        return

    rows = (db.query(DetectionEvent)
              .filter(DetectionEvent.camera_id.in_(cam_ids),
                      DetectionEvent.detection_type == "shop_open_close",
                      DetectionEvent.timestamp >= window_start_utc,
                      DetectionEvent.timestamp <  window_end_utc)
              .order_by(asc(DetectionEvent.timestamp))
              .all())

    open_ev = next((e for e in rows
                    if (e.extra or {}).get("rule") in ("shop_opened",
                                                       "shop_opened_late")),
                   None)
    # FIRST shop_closed event of the day; the maybe_emit_close_alert
    # dedupes per camera so there's typically only one anyway.
    close_ev = next((e for e in rows
                     if (e.extra or {}).get("rule") == "shop_closed"),
                    None)
    not_opened_ev = next((e for e in rows
                          if (e.extra or {}).get("rule") == "shop_not_opened"),
                         None)

    def _fmt(ev) -> str:
        ts = ev.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(tz).strftime("%H:%M")

    store_name = store.name or f"Store {store.id}"
    parts: list[str] = []
    if open_ev:
        parts.append(f"opened at {_fmt(open_ev)}")
    elif not_opened_ev:
        parts.append("did not open today")
    else:
        parts.append("opening time not recorded")
    if close_ev:
        parts.append(f"closed at {_fmt(close_ev)}")
    else:
        parts.append("closing time not recorded")

    duration_text = ""
    if open_ev and close_ev:
        delta = (close_ev.timestamp - open_ev.timestamp).total_seconds()
        mins  = max(0, int(round(delta / 60)))
        hrs, mins = divmod(mins, 60)
        if hrs and mins:
            duration_text = f"Open for {hrs} hours {mins} minutes today."
        elif hrs:
            duration_text = f"Open for {hrs} hours today."
        else:
            duration_text = f"Open for {mins} minutes today."

    summary = f"{store_name} {parts[0]}, {parts[1]}."
    if duration_text:
        summary += " " + duration_text

    # Anchor the alert on the first camera attached — the alert is
    # store-scoped conceptually, but the schema needs camera_id.
    extra = {
        "priority":           "info",
        "rule":               "shop_daily_summary",
        "store_id":           store.id,
        "store_name":         store.name,
        "message":            summary,
        "opened_at_eat":      _fmt(open_ev)  if open_ev  else None,
        "closed_at_eat":      _fmt(close_ev) if close_ev else None,
        "did_not_open":       not_opened_ev is not None and open_ev is None,
        "duration_text":      duration_text or None,
        "eat_time":           local_now.strftime("%H:%M"),
    }
    _create_info_alert(db, camera_id=cams[0].id, zone_id=None,
                       store_id=store.id,
                       detection_type="shop_open_close",
                       cls="shop_daily_summary", extra=extra)
    db.commit()

    recipients: list[str] = []
    mgr = _format_whatsapp_recipient(getattr(store, "manager_phone", None))
    if mgr:
        recipients.append(mgr)
    recipients.extend(_dashboard_recipients())
    recipients = list(dict.fromkeys(recipients))
    if recipients:
        _send_whatsapp(recipients, f"📋 {summary}")
    log.info("shop_daily_summary: store=%s %s", store.id, summary)


# ---- Occupancy-inferred store opening --------------------------------
#
# Backs up the EntryExitDetector crossing path. When the line crossing
# is missed (wrong direction, occlusion, dropped frames), we still
# need an accurate `opened_at` and an INFO alert rather than waiting
# for 09:30 EAT and firing the URGENT "not opened".
#
# Rule: ≥ PERSON_OPEN_THRESHOLD person detections across the store's
# cameras within OPEN_CONFIRMATION_WINDOW. opened_at = EARLIEST event
# in the window that triggered confirmation. Both the entrance-
# crossing path and this one share the store-level Redis marker so
# only one of them ever fires per day per store.

PERSON_OPEN_THRESHOLD       = 2          # spec
OPEN_CONFIRMATION_WINDOW_S  = 5 * 60     # 5 minutes


def confirm_opening_from_events(timestamps,
                                 *, threshold: int = PERSON_OPEN_THRESHOLD,
                                 window_seconds: int = OPEN_CONFIRMATION_WINDOW_S):
    """Pure helper — given an iterable of timestamps (assumed sorted
    ASCENDING; the caller's ORDER BY is the contract), returns the
    EARLIEST timestamp in the first window of `window_seconds` that
    contains at least `threshold` events. Returns None when no such
    window exists.

    Implementation is O(n) with a two-pointer sliding window so it
    handles a busy store's day without blowing up.

    Pure / no-IO so the unit tests can drive it directly without a
    DB or Redis stub."""
    seq = list(timestamps)
    if len(seq) < threshold:
        return None
    left = 0
    for right in range(len(seq)):
        # Shrink window from the left while it spans more than the
        # configured seconds.
        while left <= right and (
            (seq[right] - seq[left]).total_seconds() > window_seconds
        ):
            left += 1
        if (right - left + 1) >= threshold:
            return seq[left]
    return None


@celery_app.task(name="alerting.shop_open_inference_check", ignore_result=True)
def shop_open_inference_check() -> None:
    """1-min beat tick. For every active store with no opened-today
    marker yet, walk today's person events and apply the sliding-
    window rule. Fires one INFO `shop_opened_inferred` alert per
    store per day when satisfied."""
    from app.database import SessionLocal
    from app.models import Store
    from app.ai.detectors import shop_state

    r = _redis()      # used only for diagnostics; the marker write
    _ = r              # is via shop_state.mark_store_opened().

    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        for store in stores:
            try:
                _maybe_infer_open_for_store(db, store, shop_state)
            except Exception as e:
                log.exception("shop_open_inference: store=%s failed: %s",
                              store.id, e)


def _maybe_infer_open_for_store(db, store, shop_state) -> None:
    from zoneinfo import ZoneInfo
    from sqlalchemy import asc
    from app.models import DetectionEvent

    eat = ZoneInfo("Africa/Nairobi")
    now_eat = _store_eat_now(store)
    day_iso = now_eat.date().isoformat()

    # Already opened (by crossing OR by previous inference run)? Done.
    if shop_state.store_opened_today(store.id, day_iso) is not None:
        return

    # OPENING-WINDOW GATE. Store-opening inference is only meaningful
    # between earliest_open (07:00) and not_open_cutoff (09:30) EAT.
    # Outside that window we skip entirely — without the upper bound
    # this fired "Store Opened — inferred (16:52)" on ordinary
    # afternoon traffic.
    #   t < 07:00  → too early (security / cleaning)
    #   t > 09:30  → past cutoff (store already confirmed open by the
    #                crossing/occupancy paths, or genuinely not opened
    #                and handled by the URGENT not-opened check)
    cfg = shop_state._read_cfg(None)
    earliest_t = cfg.get("earliest_open_t")
    if earliest_t is None:
        from datetime import time as _time
        earliest_t = _time(7, 0)
    cutoff_t = cfg.get("not_open_cutoff_t")
    if cutoff_t is None:
        from datetime import time as _time
        cutoff_t = _time(9, 30)
    today_local_00 = now_eat.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_local = today_local_00.replace(
        hour=earliest_t.hour, minute=earliest_t.minute)
    window_end_local = today_local_00.replace(
        hour=cutoff_t.hour, minute=cutoff_t.minute)
    if now_eat < window_start_local:
        return       # too early
    if now_eat > window_end_local:
        return       # past the opening cutoff — don't infer all day

    window_start_utc = window_start_local.astimezone(timezone.utc)
    # Cap the event-scan upper bound at the cutoff (not "now") so the
    # sliding window can never include post-cutoff traffic even on a
    # late beat tick / clock skew.
    window_end_utc   = window_end_local.astimezone(timezone.utc)
    now_utc          = now_eat.astimezone(timezone.utc)
    scan_end_utc     = min(now_utc, window_end_utc)

    # ENTRANCE CAMERAS ONLY. A person detected in a stockroom or
    # behind the counter must not satisfy the "store opened" rule —
    # only crossings/detections at a drawn entrance line count.
    # Stores with no entrance line drawn are honestly excluded from
    # inference (the URGENT "Store Not Opened" still fires at the
    # cutoff if no other evidence lands).
    cam_ids = _entrance_cam_ids_for_store(db, store.id)
    if not cam_ids:
        return

    # Single chronological pull of person events for the entrance
    # cameras only — one query (no N+1) backed by the existing
    # (camera_id, timestamp) composite index.
    rows = (db.query(DetectionEvent.timestamp,
                     DetectionEvent.camera_id)
              .filter(DetectionEvent.camera_id.in_(cam_ids),
                      DetectionEvent.detection_type == "person",
                      DetectionEvent.timestamp >= window_start_utc,
                      DetectionEvent.timestamp <= scan_end_utc)
              .order_by(asc(DetectionEvent.timestamp))
              .all())
    if len(rows) < PERSON_OPEN_THRESHOLD:
        return

    # Normalise to TZ-aware EAT for the sliding window, then run the
    # pure rule.
    ts_eat = []
    for ts, _cid in rows:
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_eat.append(ts.astimezone(eat))
    opened_at = confirm_opening_from_events(ts_eat)
    if opened_at is None:
        return

    # Find which camera produced the EARLIEST contributing event so
    # the alert can anchor on a real FK + name.
    first_cam_id = None
    for ts, cid in rows:
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(eat) == opened_at:
            first_cam_id = cid
            break
    anchor_cam_id = first_cam_id or cam_ids[0]

    # Atomic claim — if another worker raced us here, bail.
    claimed = shop_state.mark_store_opened(
        store.id, day_iso,
        opened_at=opened_at,
        method="occupancy_inference",
        confidence="medium",
        camera_id=anchor_cam_id,
    )
    if not claimed:
        return

    body = (f"{store.name or 'Store ' + str(store.id)} opened — "
            f"inferred from occupancy. People detected on cameras at "
            f"{opened_at.strftime('%H:%M')} but no entrance crossing "
            f"was recorded.")
    extra = {
        "priority":          "info",
        "rule":              "shop_opened_inferred",
        "store_id":          store.id,
        "store_name":        store.name,
        "message":           body,
        "method":            "occupancy_inference",
        "confidence":        "medium",
        "opened_at_eat":     opened_at.strftime("%H:%M"),
        "opened_at_iso":     opened_at.isoformat(),
        "signal":            "occupancy_two_in_five_min",
    }
    _create_info_alert(
        db, camera_id=anchor_cam_id, zone_id=None, store_id=store.id,
        detection_type="shop_open_close", cls="shop_opened_inferred",
        extra=extra,
    )
    db.commit()
    log.info("shop_open_inference: store=%s opened_at=%s (cam=%s)",
             store.id, opened_at.isoformat(), anchor_cam_id)


# ── After-hours intrusion snapshot filmstrip ───────────────────────────
# When a person is present after the store has closed (per the store's own
# business_hours_json), fire ONE intrusion alert per store per night and
# attach up to 6 snapshots — the first immediately, then one every 5 min —
# from the best available store camera. Reuses Alert.snapshot_paths (the
# same filmstrip checkout-dwell uses), so no schema or UI change.
#
# Presence signal = recent `intrusion` DetectionEvents (produced by
# IntrusionDetector while the store is closed). Per-store session state
# lives in Redis so the beat task is stateless across worker restarts.

AFTER_HOURS_SNAP_INTERVAL_S  = int(getattr(settings, "after_hours_snapshot_interval_min", 5)) * 60
AFTER_HOURS_MAX_SNAPS        = int(getattr(settings, "after_hours_max_snapshots", 6))
AFTER_HOURS_PRESENT_GRACE_S  = int(getattr(settings, "after_hours_present_grace_sec", 150))
AFTER_HOURS_SESSION_TTL_S    = 12 * 60 * 60      # safety net; cleared when store opens
AFTER_HOURS_SNAP_RETENTION_S = 24 * 60 * 60


def _afterhours_key(store_id: int) -> str:
    return f"vg:afterhours:open:{store_id}"


def _afterhours_body(store_name: str, n: int, minutes: int) -> str:
    return (f"Someone is in {store_name} after closing time. "
            f"{n} snapshot{'s' if n != 1 else ''} captured over "
            f"{max(minutes, 0)} minute{'s' if minutes != 1 else ''}.")


def _best_store_camera_jpeg(db, store_id: int):
    """(camera_id, jpeg_bytes) for the best available camera in a store.
    Prefers cameras carrying an `entry_exit` zone (they see the door/aisle),
    then any ai-enabled camera; among those with a fresh Redis frame the
    largest JPEG wins as a rough 'clearest / highest-res' proxy. Returns
    (None, None) when nothing is streaming."""
    from app.models import Camera, Zone
    from app.stream.frame_buffer import FrameBuffer
    cams = (db.query(Camera)
              .filter(Camera.store_id == store_id, Camera.ai_enabled.is_(True))
              .all())
    if not cams:
        return None, None
    cam_ids = [c.id for c in cams]
    entry_cam_ids = {
        z.camera_id for z in db.query(Zone.camera_id, Zone.detection_types_json)
                                .filter(Zone.camera_id.in_(cam_ids)).all()
        if "entry_exit" in (z.detection_types_json or [])
    }
    fb = FrameBuffer()
    best = (None, None, -1)   # (cam_id, jpeg, score)
    for c in cams:
        jpeg = fb.latest_jpeg(c.id)
        if not jpeg:
            continue
        # entry_exit cameras get a large bonus so they win regardless of size.
        score = len(jpeg) + (10_000_000 if c.id in entry_cam_ids else 0)
        if score > best[2]:
            best = (c.id, jpeg, score)
    return best[0], best[1]


@celery_app.task(name="alerting.after_hours_intrusion_check", ignore_result=True)
def after_hours_intrusion_check() -> None:
    """Every 60s: for each closed store with a person present, create or
    extend an after-hours intrusion filmstrip (<=6 snapshots — one
    immediately, then every 5 min)."""
    if not bool(getattr(settings, "after_hours_snapshot_enabled", True)):
        return
    from app.database import SessionLocal
    from app.models import Alert, Camera, DetectionEvent, Store
    from app.ai.after_hours_snapshots import save_snapshot

    r = _redis()
    if r is None:
        return
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    present_cutoff = now - timedelta(seconds=AFTER_HOURS_PRESENT_GRACE_S)

    with SessionLocal() as db:
        for store in db.query(Store).filter(Store.is_active.is_(True)).all():
            skey = _afterhours_key(store.id)
            # Store OPEN now → end the night session (staff have arrived).
            if is_store_open(store, now):
                r.delete(skey)
                continue
            # Most recent intrusion event across this store's cameras — both
            # the "is someone here right now" signal AND the anchor camera
            # for the alert row (DetectionEvent.camera_id is NOT NULL).
            recent = (db.query(DetectionEvent)
                        .join(Camera, Camera.id == DetectionEvent.camera_id)
                        .filter(Camera.store_id == store.id,
                                DetectionEvent.detection_type == "intrusion",
                                DetectionEvent.timestamp >= present_cutoff)
                        .order_by(DetectionEvent.timestamp.desc())
                        .first())
            if recent is None:
                continue   # nobody present right now — leave session, don't snap

            try:
                raw = r.get(skey)
                sess = json.loads(raw) if raw else None
            except Exception:
                sess = None

            # ── First detection this night → new alert + snapshot #1 ──
            if sess is None:
                best_cam, jpeg = _best_store_camera_jpeg(db, store.id)
                anchor_cam = recent.camera_id      # guaranteed non-null
                extra = {
                    "priority":    "high",
                    "rule":        "after_hours_intrusion",
                    "after_hours": True,
                    "store_id":    store.id,
                    "store_name":  store.name,
                    "title":       f"Person Detected After Hours — {store.name}",
                    "message":     _afterhours_body(store.name, 1, 0),
                    "what_to_do": [
                        "Open the live camera for this store",
                        "Verify whether the person is staff or an intruder",
                        "Escalate to security / call the manager if unrecognised",
                    ],
                }
                ev = _create_info_alert(
                    db, camera_id=anchor_cam, zone_id=None, store_id=store.id,
                    detection_type="intrusion", cls="after_hours_intrusion",
                    extra=extra, capture_snapshot=True,
                )
                db.flush()
                alert = db.query(Alert).filter(Alert.event_id == ev.id).first()
                if alert is None:
                    db.commit(); continue
                path = (save_snapshot(jpeg, store_id=store.id, alert_id=alert.id,
                                      epoch_ts=int(now_ts)) if jpeg else None)
                if path:
                    alert.snapshot_paths = [path]
                db.commit()
                r.set(skey, json.dumps({
                    "alert_id":     alert.id,
                    "event_id":     ev.id,
                    "started_ts":   now_ts,
                    "last_snap_ts": now_ts if path else 0,
                    "snap_count":   1 if path else 0,
                }), ex=AFTER_HOURS_SESSION_TTL_S)
                log.info("after-hours: opened intrusion filmstrip store=%s alert=%s",
                         store.id, alert.id)
                continue

            # ── Existing session → one snapshot per 5 min, capped at 6 ──
            if int(sess.get("snap_count", 0)) >= AFTER_HOURS_MAX_SNAPS:
                continue
            if now_ts - float(sess.get("last_snap_ts") or 0) < AFTER_HOURS_SNAP_INTERVAL_S:
                continue
            _best_cam, jpeg = _best_store_camera_jpeg(db, store.id)
            if not jpeg:
                continue
            alert = db.get(Alert, int(sess["alert_id"]))
            if alert is None:
                r.delete(skey); continue
            path = save_snapshot(jpeg, store_id=store.id,
                                 alert_id=alert.id, epoch_ts=int(now_ts))
            if not path:
                continue
            paths = list(alert.snapshot_paths or [])
            paths.append(path)
            alert.snapshot_paths = paths
            n = len(paths)
            minutes = int((now_ts - float(sess.get("started_ts") or now_ts)) / 60)
            ev = db.get(DetectionEvent, int(sess["event_id"]))
            if ev is not None:
                ev.extra = {**(ev.extra or {}), "message": _afterhours_body(store.name, n, minutes)}
            db.commit()
            sess["snap_count"] = n
            sess["last_snap_ts"] = now_ts
            r.set(skey, json.dumps(sess), ex=AFTER_HOURS_SESSION_TTL_S)
            log.info("after-hours: snapshot %d/%d store=%s alert=%s",
                     n, AFTER_HOURS_MAX_SNAPS, store.id, sess["alert_id"])


@celery_app.task(name="alerting.after_hours_prune", ignore_result=True)
def after_hours_prune() -> None:
    """24h retention for after-hours filmstrips — mirrors
    prune_checkout_snapshots. Any intrusion alert carrying a filmstrip is an
    after-hours one (regular intrusion alerts have no snapshot_paths)."""
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent
    from app.ai.after_hours_snapshots import delete_alert_folder
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=AFTER_HOURS_SNAP_RETENTION_S)
    with SessionLocal() as db:
        rows = (db.query(Alert, DetectionEvent)
                  .join(DetectionEvent, DetectionEvent.id == Alert.event_id)
                  .filter(DetectionEvent.detection_type == "intrusion",
                          Alert.created_at < cutoff,
                          Alert.snapshot_paths.isnot(None))
                  .all())
        cleared = 0
        for alert, ev in rows:
            if (ev.extra or {}).get("rule") != "after_hours_intrusion":
                continue
            delete_alert_folder(int((ev.extra or {}).get("store_id") or 0), alert.id)
            alert.snapshot_paths = None
            cleared += 1
        if cleared:
            db.commit()
            log.info("after-hours prune: cleared %d filmstrips", cleared)
