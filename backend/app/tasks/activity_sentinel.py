"""Live Activity Sentinel — turns the Live Activity feed into alerts.

Purely additive consumer of the SAME ``vg:activity:{camera_id}`` Redis
keys the Live Activity tab reads (written every frame by the inference
worker: ``{"people": int, "score": float, "ts": epoch}``, TTL 300 s).
The writer, the /cameras/activity/live endpoint, and the frontend are
untouched — this module only reads.

Every 60 s the beat task:
  1. MGETs all activity blobs and appends fresh ones (ts within 300 s)
     to a capped per-camera rolling window ``vg:activity:hist:{id}``.
  2. Gathers context: camera→store map, store trading-hours state,
     fresh-frame set, tonight's intrusion sessions, and per-camera
     ``detection_configs`` overrides (detection_type="live_activity").
  3. Hands everything to the PURE ``evaluate_activity_rules`` function.
  4. Applies a Redis SET-NX dedupe bucket per trigger (10-min TTL) and
     emits surviving triggers through the existing
     ``alerting._create_info_alert`` (detection_type="live_activity").

Rules (all thresholds chain-configurable, per-camera overridable):
  activity_presence    INFO       sustained activity (people >= 5 by
                                  default — filters out passersby),
                                  2 samples; open stores only
  occupancy_surge      ATTENTION  people >= N for K consecutive samples
  store_surge          ATTENTION  store-wide sum >= M, same sustain
  after_hours_activity URGENT     people while the store is closed
                                  (suppressed when the after-hours
                                  intrusion session already fired)
  dead_scene           WARNING    fresh frames but zero detections for
                                  X minutes during open hours
                                  (X=0 disables — the default)

Dark-launch: gated on settings.activity_sentinel_enabled (default False).
Once the flag is on, coverage is OPT-OUT: every ai-enabled camera is
evaluated unless a detection_configs row (detection_type="live_activity")
explicitly sets enabled=false.
"""
from __future__ import annotations

import json
import logging
import time

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# ── TEMPORARY KILL-SWITCH (ops, Aug 2026) ──────────────────────────────────
# Hard-disables the sentinel regardless of ACTIVITY_SENTINEL_ENABLED — no
# new live_activity alerts are generated until this is flipped back to
# False (tests override it via monkeypatch so coverage stays live).
SENTINEL_TEMPORARILY_DISABLED = True

ACTIVITY_KEY_FMT = "vg:activity:{cid}"
HIST_KEY_FMT     = "vg:activity:hist:{cid}"
DEDUPE_KEY_FMT   = "vg:alert:dedupe:live_activity:{rule}:{cid}:{sid}"

HIST_MAX_SAMPLES   = 10          # ~10 min of context at the 60 s cadence
HIST_TTL_SECONDS   = 3600
SAMPLE_FRESH_S     = 300         # matches the writer's TTL
DEDUPE_TTL_SECONDS = 600         # 10-min per-(rule, camera, store) bucket

SEVERITY_BY_RULE: dict[str, str] = {
    "activity_presence":    "INFO",
    "occupancy_surge":      "ATTENTION",
    "store_surge":          "ATTENTION",
    "after_hours_activity": "URGENT",
    "dead_scene":           "WARNING",
}
# extra["priority"] vocabulary used by the existing alert body/severity
# helpers in api/alerts.py.
PRIORITY_BY_RULE: dict[str, str] = {
    "activity_presence":    "info",
    "occupancy_surge":      "warning",
    "store_surge":          "warning",
    "after_hours_activity": "high",
    "dead_scene":           "info",
}


# ── pure rule evaluator ────────────────────────────────────────────────────
def evaluate_activity_rules(
    camera_samples: dict[int, list[dict]],
    store_map: dict[int, int | None],
    config: dict,
    *,
    store_open: dict[int, bool],
    fresh_frame_cams: set[int],
    intrusion_active_stores: set[int],
    overrides: dict[int, dict] | None = None,
    now_ts: float | None = None,
) -> list[dict]:
    """Evaluate the four Live Activity Sentinel rules over rolling
    per-camera activity windows. PURE FUNCTION — no Redis, no DB, no
    clock reads (unless now_ts is omitted); every judgment is derived
    from the arguments, so tests drive it with plain dicts.

    See the approved contract in this module's docstring / the design
    plan: rules a-d and the trigger dict shape. Override semantics are
    OPT-OUT: a camera with no override row — or a row without an
    explicit enabled flag — is evaluated; ONLY an explicit
    enabled=False removes it (from everything, including store sums).
    Per-camera threshold keys override chain defaults for rules a & d;
    store-level thresholds are chain-config only in v1.

    after_hours_activity and store_surge emit ONE trigger per store;
    their camera_id anchors on the busiest evidencing camera so the
    alert thumbnail shows the scene an operator wants to see.
    """
    overrides = overrides or {}
    now = float(now_ts) if now_ts is not None else time.time()

    surge_people_default = int(config.get("surge_people", 12))
    sustain = max(1, int(config.get("surge_sustain_samples", 3)))
    store_thr = int(config.get("store_surge_people", 30))
    dead_min = int(config.get("dead_scene_minutes", 0))
    presence_on = bool(config.get("presence_enabled", False))
    presence_thr = int(config.get("presence_threshold", 5))
    presence_k = max(1, int(config.get("presence_sustain_samples", 2)))

    def _enabled(cid: int) -> bool:
        # OPT-OUT semantics: cameras are evaluated by default — no
        # override row, or a row without an explicit enabled flag,
        # both mean "enabled". ONLY an explicit enabled=False skips.
        ov = overrides.get(cid)
        return ov is None or ov.get("enabled") is not False

    def _cam_cfg(cid: int, key: str, default: int) -> int:
        ov = overrides.get(cid) or {}
        try:
            return int(ov.get(key, default))
        except (TypeError, ValueError):
            return default

    active: dict[int, list[dict]] = {
        cid: w for cid, w in camera_samples.items()
        if w and _enabled(cid)
    }
    triggers: list[dict] = []

    # ---- rule a: occupancy_surge (per camera) -------------------------
    for cid, window in active.items():
        thr = _cam_cfg(cid, "surge_people", surge_people_default)
        k = max(1, _cam_cfg(cid, "surge_sustain_samples", sustain))
        if len(window) < k:
            continue
        tail = window[-k:]
        if all(int(s.get("people") or 0) >= thr for s in tail):
            triggers.append({
                "rule": "occupancy_surge",
                "camera_id": cid,
                "store_id": store_map.get(cid),
                "severity": SEVERITY_BY_RULE["occupancy_surge"],
                "extra": {
                    "people_count": int(tail[-1].get("people") or 0),
                    "threshold": thr,
                    "sustain_samples": k,
                },
            })

    # ---- rule: activity_presence (per camera, low-threshold INFO) ------
    # Sustained activity: people >= presence_threshold (default 5 — the
    # minimum group size worth an alert; filters out passersby) for
    # presence_sustain_samples ticks. Skips stores that are explicitly
    # CLOSED (after_hours_activity owns that case at URGENT). Volume is
    # bounded by the caller's per-camera dedupe bucket.
    if presence_on:
        for cid, window in active.items():
            sid = store_map.get(cid)
            if sid is not None and store_open.get(sid) is False:
                continue
            thr = _cam_cfg(cid, "presence_threshold", presence_thr)
            k = max(1, _cam_cfg(cid, "presence_sustain_samples", presence_k))
            if len(window) < k:
                continue
            tail = window[-k:]
            if all(int(smp.get("people") or 0) >= thr for smp in tail):
                triggers.append({
                    "rule": "activity_presence",
                    "camera_id": cid,
                    "store_id": sid,
                    "severity": SEVERITY_BY_RULE["activity_presence"],
                    "extra": {
                        "people_count": int(tail[-1].get("people") or 0),
                        "threshold": thr,
                        "sustain_samples": k,
                    },
                })

    # ---- rule b: store_surge (per store, slot-aligned sums) -----------
    by_store: dict[int, list[int]] = {}
    for cid in active:
        sid = store_map.get(cid)
        if sid is not None:
            by_store.setdefault(sid, []).append(cid)
    for sid, cids in sorted(by_store.items()):
        # Slot i (1 = latest) sums each member camera's i-th-from-last
        # sample; a camera with a shorter window contributes 0 to that
        # slot — the aggregate must have been over threshold on EVERY
        # one of the last `sustain` sentinel ticks.
        slot_sums: list[int] = []
        for i in range(1, sustain + 1):
            total = 0
            for cid in cids:
                w = active[cid]
                if len(w) >= i:
                    total += int(w[-i].get("people") or 0)
            slot_sums.append(total)
        if all(t >= store_thr for t in slot_sums):
            busiest = max(
                cids, key=lambda c: int(active[c][-1].get("people") or 0))
            triggers.append({
                "rule": "store_surge",
                "camera_id": busiest,
                "store_id": sid,
                "severity": SEVERITY_BY_RULE["store_surge"],
                "extra": {
                    "people_count": slot_sums[0],
                    "threshold": store_thr,
                    "sustain_samples": sustain,
                    "camera_ids": sorted(cids),
                },
            })

    # ---- rule c: after_hours_activity (per store) ----------------------
    afterhours_by_store: dict[int, list[int]] = {}
    for cid, window in active.items():
        sid = store_map.get(cid)
        if sid is None:
            continue
        if store_open.get(sid) is not False:      # open or unknown → skip
            continue
        if sid in intrusion_active_stores:        # intrusion path owns it
            continue
        if int(window[-1].get("people") or 0) > 0:
            afterhours_by_store.setdefault(sid, []).append(cid)
    for sid, cids in sorted(afterhours_by_store.items()):
        busiest = max(cids, key=lambda c: int(active[c][-1].get("people") or 0))
        triggers.append({
            "rule": "after_hours_activity",
            "camera_id": busiest,
            "store_id": sid,
            "severity": SEVERITY_BY_RULE["after_hours_activity"],
            "extra": {
                "people_count": int(active[busiest][-1].get("people") or 0),
                "camera_ids": sorted(cids),
            },
        })

    # ---- rule d: dead_scene (per camera; disabled at 0) ----------------
    if dead_min > 0:
        for cid, window in active.items():
            cam_dead_min = _cam_cfg(cid, "dead_scene_minutes", dead_min)
            if cam_dead_min <= 0 or cid not in fresh_frame_cams:
                continue
            sid = store_map.get(cid)
            if sid is None or store_open.get(sid) is not True:
                continue
            span_s = now - float(window[0].get("ts") or now)
            if span_s < cam_dead_min * 60:
                continue
            if all(float(s.get("score") or 0.0) <= 0.0 for s in window):
                triggers.append({
                    "rule": "dead_scene",
                    "camera_id": cid,
                    "store_id": sid,
                    "severity": SEVERITY_BY_RULE["dead_scene"],
                    "extra": {
                        "window_minutes": round(span_s / 60.0, 1),
                        "threshold_minutes": cam_dead_min,
                    },
                })

    triggers.sort(key=lambda t: (t["rule"],
                                 t["store_id"] if t["store_id"] is not None else -1,
                                 t["camera_id"] if t["camera_id"] is not None else -1))
    return triggers


# ── beat task ──────────────────────────────────────────────────────────────
def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _chain_config() -> dict:
    return {
        "surge_people":          int(getattr(settings, "activity_surge_people", 12)),
        "surge_sustain_samples": int(getattr(settings, "activity_surge_sustain_samples", 3)),
        "store_surge_people":    int(getattr(settings, "activity_store_surge_people", 30)),
        "dead_scene_minutes":    int(getattr(settings, "activity_dead_scene_minutes", 0)),
        "presence_enabled":      bool(getattr(settings, "activity_presence_enabled", True)),
        "presence_threshold":    int(getattr(settings, "activity_presence_threshold", 5)),
        "presence_sustain_samples": int(getattr(
            settings, "activity_presence_sustain_samples", 2)),
    }


def _title_for(rule: str, extra: dict, store_name: str | None,
               camera_name: str | None = None) -> str:
    where = f" — {store_name}" if store_name else ""
    if rule == "activity_presence":
        return (f"Activity detected at {camera_name or 'camera'}: "
                f"{extra.get('people_count')} people present.")
    if rule == "occupancy_surge":
        return (f"Occupancy surge: {extra.get('people_count')} people "
                f"(threshold {extra.get('threshold')}){where}")
    if rule == "store_surge":
        return (f"Store-wide occupancy surge: {extra.get('people_count')} "
                f"people across cameras{where}")
    if rule == "after_hours_activity":
        return f"After-hours activity: people detected while closed{where}"
    return f"Camera activity stalled for {extra.get('window_minutes')} min{where}"


@celery_app.task(name="alerting.live_activity_sentinel", ignore_result=True,
                 soft_time_limit=50, time_limit=55)
def live_activity_sentinel() -> None:
    """60 s beat tick — see module docstring. Never raises past Celery;
    a failed run logs and the next tick retries naturally."""
    if SENTINEL_TEMPORARILY_DISABLED:
        return  # temporarily disabled — see the kill-switch constant above
    if not bool(getattr(settings, "activity_sentinel_enabled", False)):
        return
    from app.database import SessionLocal
    from app.models import Camera, DetectionConfig, Store
    from app.utils.business_hours import is_store_open

    started = time.time()
    r = _redis()

    with SessionLocal() as db:
        cams = (db.query(Camera.id, Camera.store_id, Camera.name)
                  .filter(Camera.ai_enabled == True).all())          # noqa: E712
        store_rows = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712
        override_rows = (db.query(DetectionConfig)
                           .filter(DetectionConfig.detection_type
                                   == "live_activity").all())

        store_map = {cid: sid for cid, sid, _n in cams}
        cam_names = {cid: n for cid, _sid, n in cams}
        stores_by_id = {s.id: s for s in store_rows}
        store_open = {s.id: bool(is_store_open(s)) for s in store_rows}
        # Pass `enabled` through RAW (no bool() coercion) so the
        # evaluator's opt-out rule — only an explicit False disables —
        # sees exactly what the row says. NOTE: DetectionConfig.enabled
        # has column default=False, so a threshold-only row created
        # without setting enabled WILL read as an explicit disable;
        # operators adding overrides must set enabled=true.
        overrides = {c.camera_id: {"enabled": c.enabled,
                                   **(c.extra or {})}
                     for c in override_rows if c.camera_id is not None}

        cam_ids = sorted(store_map.keys())
        if not cam_ids:
            return

        # 1. Ingest: MGET current blobs → append fresh ones to the
        #    rolling windows (RPUSH + LTRIM + EXPIRE via one pipeline).
        now = time.time()
        try:
            blobs = r.mget([ACTIVITY_KEY_FMT.format(cid=c) for c in cam_ids])
        except Exception as e:
            log.warning("activity sentinel: MGET failed: %s", e)
            return
        pipe = r.pipeline(transaction=False)
        for cid, blob in zip(cam_ids, blobs):
            if not blob:
                continue
            try:
                payload = json.loads(blob)
            except (ValueError, TypeError):
                continue
            if now - float(payload.get("ts") or 0) > SAMPLE_FRESH_S:
                continue
            key = HIST_KEY_FMT.format(cid=cid)
            pipe.rpush(key, json.dumps({
                "people": int(payload.get("people") or 0),
                "score": float(payload.get("score") or 0.0),
                "ts": float(payload.get("ts") or now),
            }))
            pipe.ltrim(key, -HIST_MAX_SAMPLES, -1)
            pipe.expire(key, HIST_TTL_SECONDS)
        try:
            pipe.execute()
        except Exception as e:
            log.warning("activity sentinel: window append failed: %s", e)

        # 2. Read windows + frame freshness + intrusion sessions.
        pipe = r.pipeline(transaction=False)
        for cid in cam_ids:
            pipe.lrange(HIST_KEY_FMT.format(cid=cid), 0, -1)
        for cid in cam_ids:
            pipe.exists(f"vg:frame:{cid}")
        active_store_ids = sorted({s for s in store_map.values() if s})
        for sid in active_store_ids:
            pipe.exists(f"vg:afterhours:open:{sid}")
        try:
            results = pipe.execute()
        except Exception as e:
            log.warning("activity sentinel: window read failed: %s", e)
            return
        n = len(cam_ids)
        camera_samples: dict[int, list[dict]] = {}
        for i, cid in enumerate(cam_ids):
            window = []
            for raw_s in results[i] or []:
                try:
                    window.append(json.loads(raw_s))
                except (ValueError, TypeError):
                    continue
            if window:
                camera_samples[cid] = window
        fresh_frame_cams = {cid for i, cid in enumerate(cam_ids)
                            if results[n + i]}
        intrusion_active = {sid for j, sid in enumerate(active_store_ids)
                            if results[2 * n + j]}

        # 3. Evaluate (pure).
        triggers = evaluate_activity_rules(
            camera_samples, store_map, _chain_config(),
            store_open=store_open,
            fresh_frame_cams=fresh_frame_cams,
            intrusion_active_stores=intrusion_active,
            overrides=overrides, now_ts=now,
        )

        # Staff/customer breakdown context for the fired triggers.
        # REALITY CHECK vs the spec: detection_events has no per-person
        # class rows and the general model has no staff/customer classes
        # — the platform's staff signal is staff_tracks (STORE-scoped,
        # no camera FK). So the breakdown is "tracks classified staff in
        # this STORE active in the last 15 min", computed ONCE per tick
        # for all triggers (tuple query, no ORM rows) and labelled with
        # its source. Any failure degrades to total-count-only alerts.
        staff_by_store: dict[int, int] = {}
        _sids = sorted({t["store_id"] for t in triggers
                        if t["store_id"] is not None})
        if _sids:
            try:
                from datetime import datetime, timedelta, timezone

                from sqlalchemy import func as _f

                from app.models import StaffTrack
                _recent = datetime.now(timezone.utc) - timedelta(minutes=15)
                staff_by_store = dict(
                    db.query(StaffTrack.store_id, _f.count(StaffTrack.id))
                      .filter(StaffTrack.store_id.in_(_sids),
                              StaffTrack.classified_as == "staff",
                              StaffTrack.last_seen >= _recent)
                      .group_by(StaffTrack.store_id).all())
            except Exception as e:
                log.warning("activity sentinel: staff breakdown query "
                            "failed (alerts fall back to totals): %s", e)

        # 4. Dedupe + emit through the existing synthetic-alert path.
        fired = 0
        for t in triggers:
            rule, cid, sid = t["rule"], t["camera_id"], t["store_id"]
            dedupe = DEDUPE_KEY_FMT.format(rule=rule, cid=cid or 0, sid=sid or 0)
            try:
                if not r.set(dedupe, "1", ex=DEDUPE_TTL_SECONDS, nx=True):
                    continue
            except Exception:
                pass                       # Redis blip → fail open (alert)
            store = stores_by_id.get(sid) if sid else None
            store_name = store.name if store else None
            extra = {
                "rule":        rule,
                "priority":    PRIORITY_BY_RULE.get(rule, "info"),
                "severity":    t["severity"],
                "store_id":    sid,
                "store_name":  store_name,
                "camera_name": cam_names.get(cid),
                "message":     _title_for(rule, t["extra"], store_name,
                                          cam_names.get(cid)),
                "source":      "live_activity_sentinel",
                **t["extra"],
            }
            _people = t["extra"].get("people_count")
            if _people is not None and sid is not None:
                _staff = min(int(staff_by_store.get(sid, 0)), int(_people))
                extra["staff_count"] = _staff
                extra["customer_count"] = int(_people) - _staff
                extra["breakdown_source"] = "staff_tracks_store_15min"
            try:
                from app.tasks.alerting import _create_info_alert
                _create_info_alert(
                    db, camera_id=cid, zone_id=None, store_id=sid,
                    detection_type="live_activity", cls=rule, extra=extra,
                )
                fired += 1
            except Exception as e:
                log.exception("activity sentinel: alert emit failed "
                              "rule=%s cam=%s store=%s: %s", rule, cid, sid, e)
                try:
                    db.rollback()
                except Exception:
                    pass
        if fired:
            db.commit()
        log.info("activity sentinel: cams=%d windows=%d triggers=%d fired=%d "
                 "(%.0f ms)", len(cam_ids), len(camera_samples), len(triggers),
                 fired, (time.time() - started) * 1000)
