"""CheckoutDwellDetector — per-customer transaction duration at counter zones.

A "checkout dwell" is the elapsed time between a track first appearing
inside a counter zone and last leaving it. It approximates a single
transaction at the till.

Sessions are filtered by the per-store thresholds in `settings`:

    settings.checkout_min_dwell_seconds  (default 30s)
    settings.checkout_max_dwell_seconds  (default 900s = 15 min)

Anything shorter than `min` is treated as a pass-through (staff walking
past, tracker glitches). Anything longer than `max` is treated as
not-a-transaction — that's a staff member working the counter for an
entire shift, which is what `staff_present` already measures.

A short-lived Redis key `vg:checkout_open:{cam}:{zone}:{track}` is
written on entry and deleted on exit so the alerting layer can detect
live sessions exceeding `settings.checkout_alert_minutes` WITHOUT
needing to share the detector's in-memory state across processes.

Co-exists with StaffPresenceDetector — same `counter` zone tag,
different output metric. The two detectors don't share state.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Any

from app.ai.detectors.base import COCO_PERSON, DetectionEvent, Detector, DetectorContext
from app.ai.zone_logic import zone_contains

log = logging.getLogger(__name__)

# Timeline-snapshot cadence — kept module-level so the session-open
# block can initialise last_snapshot_ts = -INTERVAL (sentinel that
# triggers the first capture at arrival t=0).
from app.ai.detectors.checkout_snapshots import SNAPSHOT_INTERVAL_S

# `vg:checkout_open:{cam}:{zone}:{track}` → JSON {entry_ts, store_id}
REDIS_OPEN_KEY_FMT = "vg:checkout_open:{cam}:{zone}:{track}"
# Key expires automatically so the alerting beat task never picks up
# stale rows from a crashed worker. Set generously above the max
# session length.
REDIS_OPEN_TTL_SECONDS = 30 * 60       # 30 min

# Time after which a track that's been "in the zone" but not seen on any
# frame is considered to have exited (tracker dropouts happen). Tighter
# than REDIS_OPEN_TTL because we still want a timely session-close.
TRACK_LOST_GRACE_SECONDS = 60.0


def _redis():
    """Optional Redis. Detector still emits metric_snapshots when Redis
    is unavailable; only the live-alert path needs the key set."""
    try:
        import redis
        from app.config import settings
        return redis.from_url(settings.redis_url)
    except Exception:
        return None


class CheckoutDwellDetector(Detector):
    """Per-track-per-counter-zone session timer.

    On every evaluate():
      • For each (track, counter-zone) pair where the centroid is now
        IN the zone — if this is the first sighting, record entry +
        publish a Redis open-session key.
      • For each previously-open session whose track is NOT inside any
        counter zone this frame OR whose track hasn't been seen for >
        TRACK_LOST_GRACE_SECONDS, close the session: emit a
        DetectionEvent + write `checkout_dwell_seconds` to
        metric_snapshots (filtered by the min/max thresholds).
      • Periodically sweep expired Redis open-session keys (the TTL
        handles abandoned-by-crashed-worker rows).
    """

    detection_type = "checkout_dwell"
    needs_tracking = True

    def __init__(self) -> None:
        # (camera_id, track_id, zone_id) → {entry_ts, last_seen_ts, store_id}
        self._sessions: dict[tuple[int, int, int], dict[str, Any]] = {}
        # Per-camera "last evaluate" tick — used to GC sessions whose
        # tracks have aged out without our seeing an explicit exit.
        self._last_tick: dict[int, float] = {}

    # ------------------------------------------------------------------

    def _thresholds(self) -> tuple[float, float]:
        from app.config import settings
        lo = float(getattr(settings, "checkout_min_dwell_seconds", 30))
        hi = float(getattr(settings, "checkout_max_dwell_seconds", 900))
        return lo, hi

    def _publish_open(self, r, cam: int, zone: int, track: int,
                       entry_ts: float, store_id: int | None,
                       *, min_alert_seconds: int = 0,
                       last_seen_ts: float | None = None) -> None:
        if r is None:
            return
        try:
            r.set(
                REDIS_OPEN_KEY_FMT.format(cam=cam, zone=zone, track=track),
                json.dumps({
                    "entry_ts":          float(entry_ts),
                    "store_id":          store_id,
                    "camera_id":         cam,
                    "zone_id":           zone,
                    "track_id":          track,
                    # Heartbeat consumed by the alerting task. An entry-only
                    # key can outlive a crashed inference worker and falsely
                    # look like a customer who is still waiting.
                    "last_seen_ts":      float(last_seen_ts or entry_ts),
                    # Floor used by alerting.checkout_long_session_check
                    # to override the global threshold for sessions where
                    # the participant looks like medium-confidence staff
                    # (longer grace before raising attention).
                    "min_alert_seconds": int(min_alert_seconds),
                }),
                ex=REDIS_OPEN_TTL_SECONDS,
            )
        except Exception as e:
            log.debug("checkout: publish-open failed: %s", e)

    def _clear_open(self, r, cam: int, zone: int, track: int) -> None:
        if r is None:
            return
        try:
            r.delete(REDIS_OPEN_KEY_FMT.format(cam=cam, zone=zone, track=track))
        except Exception:
            pass

    def _close_session(self, ctx: DetectorContext, key: tuple[int, int, int],
                       exit_ts: float, r) -> DetectionEvent | None:
        sess = self._sessions.pop(key, None)
        if sess is None:
            return None
        cam, track, zone = key
        # Snapshot finalisation BEFORE we clear the open-session key —
        # _finalise_snapshots reads alert_id from it. Promotes pending
        # frames to the alert folder when the alerter linked an alert,
        # discards them otherwise. Runs on every close path (including
        # under-min and over-max-dwell drops below).
        self._finalise_snapshots(cam, track, zone, r)
        self._clear_open(r, cam, zone, track)
        duration = float(exit_ts - sess["entry_ts"])
        lo, hi = self._thresholds()
        if duration < lo:
            # Pass-through — staff walking past, tracker glitch, etc.
            return None
        if duration > hi:
            # Staff working the counter for hours — not a checkout
            # transaction. `staff_present` measures this separately.
            log.info("checkout: dropped over-long session cam=%s zone=%s "
                     "track=%s duration=%.0fs (> %.0fs)",
                     cam, zone, track, duration, hi)
            return None

        # Write the metric as one distinct row per session using `dims`
        # so multiple completions in the same 1-minute bucket don't
        # overwrite each other under aggregator="last".
        if ctx.db is not None:
            from app.analytics import recorder
            recorder.record(
                ctx.db, "checkout_dwell_seconds", duration,
                camera_id=cam, store_id=sess.get("store_id"),
                zone_id=zone,
                dims={"session_id": f"{track}:{int(sess['entry_ts'])}"},
                aggregator="last",
            )

        return DetectionEvent(
            detection_type=self.detection_type,
            cls="checkout_complete",
            confidence=1.0,
            bbox_norm=[0.0, 0.0, 1.0, 1.0],
            track_id=track,
            zone_id=zone,
            extra={
                "dwell_seconds": round(duration, 1),
                "entry_ts":      sess["entry_ts"],
                "exit_ts":       exit_ts,
                "store_id":      sess.get("store_id"),
            },
        )

    # ------------------------------------------------------------------

    def _finalise_snapshots(self, cam: int, track: int, zone: int, r) -> None:
        """At session close: if the alerter linked an alert_id to
        this session via the Redis open-session payload, move any
        remaining pending JPEGs into the alert folder and atomically
        extend Alert.snapshot_paths. Otherwise discard the pending
        folder so we don't leak disk on routine queueing sessions."""
        from app.ai.detectors.checkout_snapshots import (
            promote_to_alert, discard_pending, delete_alert_folder,
        )
        alert_id: int | None = None
        if r is not None:
            try:
                raw = r.get(REDIS_OPEN_KEY_FMT.format(
                    cam=cam, zone=zone, track=track))
                if raw:
                    payload = json.loads(raw)
                    alert_id = payload.get("alert_id")
            except Exception:
                alert_id = None
        if not alert_id:
            discard_pending(cam, track, zone)
            return
        new_paths = promote_to_alert(
            camera_id=cam, track_id=track, zone_id=zone,
            alert_id=int(alert_id),
        )
        if not new_paths:
            return
        # Append to the alert row, dedup'd against what the alerter
        # already wrote at fire time. Best-effort: a DB failure must
        # not crash the inference loop.
        try:
            from app.database import SessionLocal
            from app.models import Alert
            with SessionLocal() as db:
                a = db.get(Alert, int(alert_id))
                if a is None:
                    # Alert deleted (retention prune?) — clean files.
                    delete_alert_folder(int(alert_id))
                    return
                existing = list(a.snapshot_paths or [])
                seen = set(existing)
                for p in new_paths:
                    if p not in seen:
                        existing.append(p)
                a.snapshot_paths = existing
                db.commit()
        except Exception:
            log.exception("checkout snapshot append failed alert=%s",
                          alert_id)

    # ------------------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        # The till point ONLY. A zone qualifies when it is tagged
        # `counter` AND is not tagged with anything that would make
        # a long presence there normal:
        #   - `staff_zone` / `staff_area`: staff working behind the
        #     counter all shift is `staff_present`, not a transaction
        #   - `queue`: queue-wait-time is QueueDetector's job
        # The legacy operator-drawn "counter" zones with no modifier
        # tags are the only ones we want to time.
        EXCLUDE_TAGS = {"staff_zone", "staff_area", "queue"}
        counter_zones = [
            z for z in ctx.zones
            if "counter" in (z.get("detection_types_json") or [])
            and not (EXCLUDE_TAGS & set(z.get("detection_types_json") or []))
        ]
        if not counter_zones:
            return []
        now = ctx.timestamp or time.time()
        self._last_tick[ctx.camera_id] = now
        r = _redis()
        out: list[DetectionEvent] = []

        # Person tracks only — exclude non-person classes early.
        person_tracks = [
            (tr, det) for (tr, det) in (ctx.tracks or [])
            if det.get("cls") in COCO_PERSON
        ]

        # Per-frame cache: classify each track at most once even when
        # multiple counter zones contain the same person. classify()
        # is cheap relative to YOLO inference but the HSV sampling
        # still touches the frame pixels, so caching matters on busy
        # multi-zone tills.
        from app.ai.detectors import staff_identity
        # Cache the full verdict dict (not just level) because the
        # suppression rule below reads `in_pure_staff_zone_now` as
        # well as `level`.
        verdict_cache: dict[int, dict] = {}

        def _verdict_for(track_id: int, det: dict) -> dict:
            """Return the staff_identity verdict for this track this
            frame. Failure → empty dict (treated as level='unknown' /
            in_pure_staff_zone_now=False), matching the spec's "if
            classify fails treat as unknown" rule."""
            cached = verdict_cache.get(track_id)
            if cached is not None:
                return cached
            try:
                v = staff_identity.classify(ctx, det, track_id, now) or {}
            except Exception:
                v = {}
            verdict_cache[track_id] = v
            return v

        # Pass 1 — observe current frame, open new sessions, refresh
        # last_seen_ts on continuing ones.
        # P4: PolygonZone (foot-point) containment when a frame is available.
        _wh = None
        if ctx.frame_bgr is not None:
            _h, _w = ctx.frame_bgr.shape[:2]
            _wh = (_w, _h)
        active_keys: set[tuple[int, int, int]] = set()
        for tr, det in person_tracks:
            track_id = getattr(tr, "track_id", None)
            if track_id is None:
                continue
            bbox = det.get("bbox_norm")
            if not bbox:
                continue
            for z in counter_zones:
                poly = z.get("polygon_coords_json")
                if not poly:
                    continue
                if not zone_contains(bbox, poly, zone_id=z["id"], frame_wh=_wh):
                    continue
                key = (ctx.camera_id, int(track_id), int(z["id"]))

                # Never time STAFF as a checkout. We drop the session only
                # on RELIABLE staff markers a customer can't trigger, so a
                # dark-clothed CUSTOMER — who reads as HIGH via uniform+dwell
                # after 5 min at the counter (the f8ee2e8 regression) and is
                # exactly the long-checkout we must alert on — is NOT dropped:
                #   • physically inside a staff_zone / staff_area polygon
                #     (customers stand on the customer side, never in it), OR
                #   • HIGH classification grounded in a visible lanyard
                #     (customers don't wear store lanyards).
                # A bare HIGH at a pure counter (uniform colour + dwell) is
                # NOT treated as staff — that's the stuck-customer case.
                v = _verdict_for(int(track_id), det)
                level = v.get("level") or "unknown"
                in_pure_staff_zone = bool(v.get("in_pure_staff_zone_now"))
                has_lanyard = bool(v.get("has_lanyard"))
                is_staff = in_pure_staff_zone or (level == "high" and has_lanyard)
                if is_staff:
                    if key in self._sessions:
                        self._sessions.pop(key, None)
                        self._clear_open(r, ctx.camera_id, int(z["id"]),
                                          int(track_id))
                        log.debug("checkout: dropped session for staff "
                                  "(pure_staff_zone=%s lanyard=%s level=%s) "
                                  "cam=%s zone=%s track=%s",
                                  in_pure_staff_zone, has_lanyard, level,
                                  ctx.camera_id, z["id"], track_id)
                    continue   # not in active_keys → close logic skipped

                # MEDIUM-staff sessions get a configurable 12-minute floor
                # so it actually exceeds the global 8-minute default.
                from app.config import settings as _settings
                min_alert = (int(getattr(
                    _settings, "checkout_medium_staff_alert_minutes", 12)) * 60
                    if level == "medium" else 0)

                sess = self._sessions.get(key)
                if sess is None:
                    # P5: require an ESTABLISHED track before opening a session
                    # — a fleeting detection / tracker glitch must not start a
                    # checkout timer. len(history) works for both ByteTrack and
                    # the IOUTracker fallback. Threshold from settings.
                    from app.config import settings as _s
                    if len(getattr(tr, "history", []) or []) < int(_s.checkout_min_frames):
                        continue
                active_keys.add(key)
                if sess is None:
                    sess = {"entry_ts": now, "last_seen_ts": now,
                            "store_id": ctx.store_id,
                            "min_alert_seconds": min_alert,
                            "staff_level": level,
                            "last_redis_refresh_ts": 0.0,
                            # Last 60s-snapshot timestamp (Q1 spec —
                            # gated on SESSION time, not inference fps).
                            # Sentinel `-INTERVAL` so the FIRST evaluate()
                            # tick captures arrival (now - (-60) >= 60),
                            # honouring "from the first time the customer
                            # is at the checkout queue" per Q2.
                            "last_snapshot_ts": -SNAPSHOT_INTERVAL_S,
                            # Carried for _finalise_snapshots without
                            # destructuring the dict key tuple again.
                            "track_id": int(track_id),
                            "zone_id":  int(z["id"])}
                    self._sessions[key] = sess
                    self._publish_open(r, ctx.camera_id, int(z["id"]),
                                        int(track_id), now, ctx.store_id,
                                        min_alert_seconds=min_alert,
                                        last_seen_ts=now)
                    sess["last_redis_refresh_ts"] = now
                    log.debug("checkout: open cam=%s zone=%s track=%s "
                              "level=%s", ctx.camera_id, z["id"],
                              track_id, level)
                else:
                    sess["last_seen_ts"] = now
                    # Upgrade an existing session to the MEDIUM floor
                    # if the track has since been re-classified — the
                    # Redis payload needs to track the change so the
                    # alerter sees the new threshold.
                    if min_alert > sess.get("min_alert_seconds", 0):
                        sess["min_alert_seconds"] = min_alert
                        sess["staff_level"] = level
                        self._publish_open(
                            r, ctx.camera_id, int(z["id"]),
                            int(track_id), sess["entry_ts"], ctx.store_id,
                            min_alert_seconds=min_alert,
                            last_seen_ts=now)
                        sess["last_redis_refresh_ts"] = now
                    elif now - float(sess.get("last_redis_refresh_ts", 0.0)) >= 5.0:
                        self._publish_open(
                            r, ctx.camera_id, int(z["id"]), int(track_id),
                            sess["entry_ts"], ctx.store_id,
                            min_alert_seconds=int(sess.get("min_alert_seconds", 0)),
                            last_seen_ts=now)
                        sess["last_redis_refresh_ts"] = now

                # Per-spec Q1: one snapshot every 60 SESSION seconds
                # (not every inference cycle — at 2 fps the cycle is
                # 0.5s; gating here would write 120 snaps/min/session).
                from app.ai.detectors.checkout_snapshots import capture_pending
                if (now - sess.get("last_snapshot_ts", 0.0)) >= SNAPSHOT_INTERVAL_S:
                    written = capture_pending(
                        ctx.frame_bgr,
                        camera_id=ctx.camera_id,
                        track_id=int(track_id),
                        zone_id=int(z["id"]),
                        epoch_ts=int(now),
                    )
                    if written is not None:
                        sess["last_snapshot_ts"] = now

        # Pass 2 — close any session whose track is no longer in its
        # zone OR has aged out beyond TRACK_LOST_GRACE_SECONDS. We
        # only inspect sessions belonging to THIS camera — other
        # cameras' sessions are owned by their own detector instances.
        to_close: list[tuple[tuple[int, int, int], float]] = []
        for key, sess in list(self._sessions.items()):
            cam, _track, _zone = key
            if cam != ctx.camera_id:
                continue
            if key in active_keys:
                continue        # still present
            if (now - sess["last_seen_ts"]) >= TRACK_LOST_GRACE_SECONDS:
                to_close.append((key, sess["last_seen_ts"]))
        for key, exit_ts in to_close:
            ev = self._close_session(ctx, key, exit_ts, r)
            if ev is not None:
                out.append(ev)

        return out
