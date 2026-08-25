"""Priority-1 retail detectors:

  - QueueDetector         — counts people in queue-tagged zones,
                            writes queue_length + queue_wait_seconds metrics,
                            fires an alert when the queue exceeds threshold
  - OccupancyMetricsDetector — extends the existing OccupancyDetector
                            with metric writes + capacity-based alerts
  - UniqueVisitorDetector — entry-zone-driven daily dedup of visitors
  - IntrusionDetector     — person/motion outside business hours in a
                            restricted zone → high-priority alert

All four use `ctx.db` for metric writes; without a DB session attached
they degrade gracefully (events still fire, no metrics persisted).
"""
from __future__ import annotations
import hashlib
import logging
import time
from datetime import date

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone
from app.utils.business_hours import (
    intrusion_time_context, is_open_with_default, localised_now,
)

log = logging.getLogger(__name__)


def _insert_unique_visitor(db, **values) -> None:
    """Persist a visitor once without racing concurrent camera tasks.

    A process-local seen set is only an optimisation: retries and overlapping
    workers can still reach the database together. Use the database's unique
    key as the authority and make a duplicate a no-op instead of rolling back
    other detector work in the shared session.
    """
    from app.models import VisitorTrack

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - deployed and test dialects are handled above
        db.add(VisitorTrack(**values))
        db.flush()
        return

    stmt = (insert(VisitorTrack.__table__)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["store_id", "day", "track_signature"],
            ))
    db.execute(stmt)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class QueueDetector(Detector):
    """Counts people inside any zone tagged `queue` and writes the bucket
    metric. Optionally fires an alert when count >= `crowd_threshold`.

    Wait time is computed by the tracker: for any person track that
    sits inside the queue zone, the dwell duration is averaged and
    pushed to `queue_wait_seconds`.

    Also renders a Lumana-style queue overlay onto the latest frame —
    numbered colour-coded boxes (🟢/🟡/🔴 by wait), per-person wait
    labels, a dashed flow line from #1 to #N, and a summary banner at
    the bottom — and publishes it to vg:frame_overlay:{camera_id} for
    the Live View and snapshot endpoints to prefer over the raw frame.
    """

    detection_type = "queue"
    needs_tracking = True

    # BGR (OpenCV order). Spec values.
    _RED   = (0,   0,   255)
    _AMBER = (0,   165, 255)
    _GREEN = (0,   255, 0)
    _WHITE = (255, 255, 255)
    _BLACK = (0,   0,   0)
    # Wait-time bands in seconds.
    _GREEN_MAX = 120   # < 2 min
    _AMBER_MAX = 240   # 2-4 min → amber, > 4 min → red

    def __init__(self):
        self._entered: dict[tuple[int, int, int], float] = {}   # (cam, track, zone) → t0
        self._fired:   dict[int, float] = {}                    # zone_id → last alert ts

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        threshold = int(cfg.get("crowd_threshold") or 5)
        thr_conf  = float(cfg.get("confidence_threshold", 0.5))
        zones = [z for z in ctx.zones
                 if "queue" in (z.get("detection_types_json") or [])
                 and not z.get("suppressed")]
        if not zones:
            return []

        # Persons crossing confidence — used both for the headcount and
        # the per-track dwell measurement.
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]

        out: list[DetectionEvent] = []
        now = time.time()
        # Per-person queue entries across ALL queue zones — used to
        # render the overlay once at the end of the loop.
        queue_entries: list[dict] = []
        for z in zones:
            in_zone = [p for p in people if bbox_in_zone(p["bbox_norm"], z["polygon_coords_json"])]
            count = len(in_zone)

            # Track-level dwell to estimate wait time + collect per-
            # person bbox/wait for the overlay.
            track_dwells: list[float] = []
            for tr, _det in ctx.tracks:
                if tr.cls not in COCO_PERSON:
                    continue
                if not bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    self._entered.pop((ctx.camera_id, tr.track_id, z["id"]), None)
                    continue
                key = (ctx.camera_id, tr.track_id, z["id"])
                t0 = self._entered.setdefault(key, now)
                wait = max(0.0, now - t0)
                track_dwells.append(wait)
                queue_entries.append({
                    "track_id": tr.track_id, "bbox": tr.bbox_norm,
                    "wait": wait, "t0": t0, "zone_id": z["id"],
                })

            avg_wait = (sum(track_dwells) / len(track_dwells)) if track_dwells else 0.0

            # Metric writes (best effort).
            if ctx.db is not None:
                from app.analytics import recorder
                recorder.record(ctx.db, "queue_length", float(count),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=z["id"], aggregator="max")
                if track_dwells:
                    recorder.record(ctx.db, "queue_wait_seconds", avg_wait,
                                    camera_id=ctx.camera_id, store_id=ctx.store_id,
                                    zone_id=z["id"], aggregator="avg")

            # Alert promotion — dedup per zone, every 60s max.
            if count >= threshold and now - self._fired.get(z["id"], 0) > 60:
                self._fired[z["id"]] = now
                # Use the densest person's bbox as the alert thumbnail target.
                anchor = max(in_zone, key=lambda p: p["conf"])
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="queue",
                    confidence=min(1.0, count / max(threshold, 1)),
                    bbox_norm=anchor["bbox_norm"], zone_id=z["id"],
                    extra={"count": count, "threshold": threshold,
                           "avg_wait_seconds": round(avg_wait, 1)},
                ))

        # Render + publish the queue overlay. Best-effort — a failed
        # render must not break the detection chain.
        if queue_entries and ctx.frame_bgr is not None:
            try:
                self._publish_overlay(ctx, queue_entries, len(zones))
            except Exception:
                import logging as _l
                _l.getLogger(__name__).exception(
                    "queue overlay render failed for camera %s", ctx.camera_id)
        return out

    # ---- Overlay rendering ----------------------------------------

    def _band_colour(self, wait: float) -> tuple[int, int, int]:
        if wait < self._GREEN_MAX:
            return self._GREEN
        if wait < self._AMBER_MAX:
            return self._AMBER
        return self._RED

    @staticmethod
    def _mmss(seconds: float) -> str:
        s = int(round(max(0.0, seconds)))
        return f"{s // 60:02d}:{s % 60:02d}"

    def _publish_overlay(self, ctx: DetectorContext, entries: list[dict],
                         active_counters: int) -> None:
        """Draw numbered boxes + dashed flow line + summary banner on
        the current frame and stash it under vg:frame_overlay:{cam}
        for the snapshot/Live View endpoints."""
        import cv2
        import redis as _redis
        from app.config import settings

        # Sort longest-waiting first → that's queue position #1.
        ordered = sorted(entries, key=lambda e: e["t0"])
        img = ctx.frame_bgr.copy()
        h, w = img.shape[:2]

        # Centroids in pixel space, used both for the dashed line and
        # for the box-corner label.
        centroids: list[tuple[int, int]] = []

        for i, e in enumerate(ordered, start=1):
            x1, y1, x2, y2 = e["bbox"]
            p1 = (int(max(0, x1) * w), int(max(0, y1) * h))
            p2 = (int(min(1, x2) * w), int(min(1, y2) * h))
            if p2[0] <= p1[0] or p2[1] <= p1[1]:
                continue
            cx = (p1[0] + p2[0]) // 2
            cy = (p1[1] + p2[1]) // 2
            centroids.append((cx, cy))

            colour = self._band_colour(e["wait"])
            cv2.rectangle(img, p1, p2, colour, 3)
            label = f"#{i} | {self._mmss(e['wait'])}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            # Label badge: filled box just above the bbox so the white
            # text reads on any background.
            ly = max(0, p1[1] - th - 6)
            cv2.rectangle(img, (p1[0], ly), (p1[0] + tw + 8, ly + th + 6),
                          self._BLACK, -1)
            cv2.rectangle(img, (p1[0], ly), (p1[0] + tw + 8, ly + th + 6),
                          colour, 1)
            cv2.putText(img, label, (p1[0] + 4, ly + th + 1),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, self._WHITE, 2, cv2.LINE_AA)

        # Dashed flow line #1 → #2 → … → #N with an arrowhead at the end.
        if len(centroids) >= 2:
            for a, b in zip(centroids, centroids[1:]):
                self._dashed_line(img, a, b, self._WHITE, thickness=2, dash=14, gap=8)
            # Arrowhead on the final segment so direction reads instantly.
            cv2.arrowedLine(img, centroids[-2], centroids[-1],
                            self._WHITE, 2, tipLength=0.20)

        # Bottom summary banner.
        waits = [e["wait"] for e in ordered]
        avg = sum(waits) / len(waits)
        peak = max(waits)
        peak_emoji = ("🔴" if peak >= self._AMBER_MAX
                      else "🟡" if peak >= self._GREEN_MAX else "🟢")
        summary = (f"Queue: {len(ordered)} people | "
                   f"Avg wait: {self._mmss(avg)} | "
                   f"Longest: {self._mmss(peak)} {peak_emoji} | "
                   f"Counter: {active_counters} active")
        bar_h = max(28, h // 22)
        overlay = img.copy()
        cv2.rectangle(overlay, (0, h - bar_h), (w, h), self._BLACK, -1)
        cv2.addWeighted(overlay, 0.6, img, 0.4, 0, img)
        scale = max(0.5, min(0.9, w / 1400))
        cv2.putText(img, summary, (10, h - int(bar_h * 0.28)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, self._WHITE,
                    max(1, int(scale * 2)), cv2.LINE_AA)

        # Encode + publish. 30s TTL matches the streamer's vg:frame
        # TTL so a stale overlay never lingers when the queue empties.
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        r = _redis.from_url(settings.redis_url)
        r.set(f"vg:frame_overlay:{ctx.camera_id}".encode(),
              jpg.tobytes(), ex=30)

    @staticmethod
    def _dashed_line(img, p1, p2, colour, thickness=2, dash=14, gap=8) -> None:
        """OpenCV has no native dashed line — step along the segment
        and draw short solid pieces with gaps between them."""
        import numpy as np
        x1, y1 = p1; x2, y2 = p2
        dx, dy = x2 - x1, y2 - y1
        length = float(np.hypot(dx, dy))
        if length < 1:
            return
        ux, uy = dx / length, dy / length
        period = dash + gap
        steps = int(length // period)
        for k in range(steps + 1):
            s = k * period
            e = min(length, s + dash)
            sx, sy = int(x1 + ux * s), int(y1 + uy * s)
            ex, ey = int(x1 + ux * e), int(y1 + uy * e)
            import cv2 as _cv2
            _cv2.line(img, (sx, sy), (ex, ey), colour, thickness, _cv2.LINE_AA)


# ---------------------------------------------------------------------------
# Occupancy extension — adds metric writes + store-capacity alert
# ---------------------------------------------------------------------------

class OccupancyMetricsDetector(Detector):
    """Always-on KPI writer. Counts people in the frame each tick and
    writes the `occupancy` metric. Not gated on operator toggles — the
    inference worker forces this detector on for every camera.

    Three earlier bugs fixed here:
      1. It used to require `config["occupancy"].enabled=True`. Most
         cameras don't have a row for "occupancy" so it never wrote a
         single metric.
      2. The aggregator was `last`, which means the value of the LAST
         frame in the 1-minute bucket overwrites the rest. If the last
         frame happened to be empty (very common: someone stepped out)
         the bucket showed 0 even though the room was busy moments
         earlier. Switched to `max` — peak occupancy in the bucket is
         what every dashboard actually wants.
      3. Threshold was 0.5 but YOLO's call-time conf is 0.25 and
         alert-side detectors used 0.4. The metric saw fewer people
         than the rest of the system. Now reads from cfg if set, else
         defaults to 0.4.
    """

    detection_type = "occupancy_metrics"

    def __init__(self):
        self._fired_cap_at = 0.0

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        # Always run; the inference loader sets enabled=True for us.
        thr_conf = float(((ctx.config.get("occupancy_metrics") or {})
                          .get("confidence_threshold")) or 0.4)
        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        count = len(people)

        if ctx.db is not None:
            from app.analytics import recorder
            # `max` so a busy minute is captured even if the very last
            # frame in the bucket happened to be empty.
            recorder.record(ctx.db, "occupancy", float(count),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            aggregator="max")

        # Capacity alert — once per minute when exceeded.
        out: list[DetectionEvent] = []
        if ctx.store_id is not None and ctx.db is not None and count > 0:
            from app.models import Store
            store = ctx.db.get(Store, ctx.store_id)
            cap = store.capacity if store else None
            now = time.time()
            if cap and count >= cap and now - self._fired_cap_at > 60:
                self._fired_cap_at = now
                out.append(DetectionEvent(
                    detection_type="occupancy", cls="capacity",
                    confidence=min(1.0, count / cap), bbox_norm=[0, 0, 1, 1],
                    extra={"count": count, "capacity": cap, "store_id": ctx.store_id},
                ))
        return out


# ---------------------------------------------------------------------------
# Unique visitors — entry-zone driven daily dedup
# ---------------------------------------------------------------------------

class UniqueVisitorDetector(Detector):
    """Always-on per-camera daily dedup of visitors.

    Earlier this detector required an `entry`-tagged zone. Per the
    overhaul Rule 1 it now runs on every camera unconditionally: any
    person track seen today on this camera contributes one visitor row.
    Dedup key = (store_id, day, "cam{id}:tr{track_id}").

    Known limitation: without a ReID model, the same person passing
    two cameras in one store is counted twice. Tag ONE camera at each
    store with `entry_exit` zones for the most accurate single-source
    of truth (see EntryExitDetector); leave the others as ambient
    contribution to occupancy and heatmap.
    """

    detection_type = "unique_visitor"
    needs_tracking = True

    def __init__(self):
        self._seen_today: set[tuple] = set()

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        # Always-on per Rule 1; inference loader force-enables this type.
        if ctx.db is None:
            return []

        today = date.today()
        for tr, det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue

            signature = f"cam{ctx.camera_id}:tr{tr.track_id}"
            cache_key = (ctx.store_id, today, signature)
            if cache_key in self._seen_today:
                continue
            # Sprint 2.2: stamp the cross-camera identity if the Re-ID
            # loop assigned one to this track (Track.extra carries it).
            # NULL when Re-ID is disabled — counting then falls back to
            # the per-camera track_signature as before.
            global_person_id = (tr.extra or {}).get("global_person_id")

            try:
                _insert_unique_visitor(
                    ctx.db,
                    store_id=ctx.store_id,
                    camera_id=ctx.camera_id,
                    day=today,
                    track_signature=signature,
                    global_person_id=global_person_id,
                )
                ctx.db.flush()
                self._seen_today.add(cache_key)
            except Exception:
                ctx.db.rollback()
        return []


# ---------------------------------------------------------------------------
# Intrusion — outside-business-hours person/motion in restricted zones
# ---------------------------------------------------------------------------

class IntrusionDetector(Detector):
    """Fires a *high-priority* DetectionEvent when a person is seen
    inside a `restricted` zone while the store is closed.

    Closed = `is_open_with_default(store.business_hours, local_now)
    == False` — unconfigured / malformed hours fall back to the default
    Vivo window (09:00-21:00) so a store nobody configured is armed
    OVERNIGHT, not around the clock (Aug-2026 false-urgent fix; an
    explicitly empty day like `"sun": []` still arms all day).
    Without a store assignment we fall back to a global
    `extra.always_armed` flag (useful for back-alley cameras).
    """

    detection_type = "intrusion"

    def __init__(self):
        # One incident per (camera, track, zone).  The previous zone-only
        # 30-second timer repeatedly alerted on the same person standing in
        # one place.  A fired identity now re-arms only after it has been
        # continuously absent from the protected zone for the configured gap.
        self._fired: set[tuple[int, int, int]] = set()
        self._last_seen: dict[tuple[int, int, int], float] = {}

    @staticmethod
    def _scene_fingerprint(bbox_norm: list[float], bucket: float = 0.02) -> str:
        """Stable, non-identifying position fingerprint across task restarts.

        Tracker IDs restart with each inference task.  Quantising the bounding
        box absorbs normal detector jitter while retaining enough spatial
        separation not to collapse unrelated parts of a camera view.
        """
        safe_bucket = max(0.005, float(bucket))
        quantised = ":".join(
            str(int(round(float(value) / safe_bucket)))
            for value in bbox_norm
        )
        return hashlib.sha256(quantised.encode()).hexdigest()[:16]

    @classmethod
    def _persistent_incident_allowed(
        cls, ctx: DetectorContext, zone_id: int, bbox_norm: list[float],
        rearm_seconds: float, fingerprint_bucket: float,
    ) -> bool:
        """Atomically open an incident, or refresh an existing one's TTL.

        The key contains camera, zone and a one-way coarse position digest --
        never an image or biometric identifier.  A continuously visible fixed
        object therefore keeps the incident closed across short-lived worker
        tasks.  After genuine absence, Redis expires the key and the same area
        can alert again.  Redis failure falls back to the detector's in-memory
        gate, limiting the failure to one alert per running task.
        """
        try:
            import redis
            from app.config import settings

            fingerprint = cls._scene_fingerprint(
                bbox_norm, bucket=fingerprint_bucket)
            key = (f"vg:intrusion:incident:{ctx.camera_id}:"
                   f"{zone_id}:{fingerprint}")
            ttl = max(1, int(rearm_seconds))
            client = redis.from_url(settings.redis_url)
            if client.set(key, "1", ex=ttl, nx=True):
                return True
            # Refresh while the same scene remains visible.  The marker only
            # expires after a full re-arm interval with no matching detection.
            client.expire(key, ttl)
            return False
        except Exception as exc:
            log.warning(
                "intrusion persistent dedupe unavailable cam=%s zone=%s: %s; "
                "using in-process fallback", ctx.camera_id, zone_id, exc,
            )
            return True

    def _within_after_hours_grace(self, ctx: DetectorContext) -> bool:
        """True when the current store-local time is inside the
        configurable opening / closing grace window around today's
        trading hours. Used to decide whether a staff-identified
        person after-hours is "opening / closing" (fully suppressed)
        or "present at an unusual time" (downgraded ATTENTION alert).

        Permissive default — when business_hours_json is missing OR
        unparseable, returns True so we err toward NOT firing URGENT
        on a possibly-legitimate staff arrival."""
        if ctx.business_hours is None or not ctx.store_timezone:
            return True
        try:
            from app.config import settings as _s
            grace_before = int(getattr(_s, "person_afterhours_grace_before_min", 60))
            grace_after  = int(getattr(_s, "person_afterhours_grace_after_min",  60))
            local = localised_now(ctx.store_timezone)
            from app.utils.business_hours import WEEKDAY_KEYS, _parse_window
            windows = ctx.business_hours.get(
                WEEKDAY_KEYS[local.weekday()]) or []
            t_min = local.hour * 60 + local.minute
            for w in windows:
                parsed = _parse_window(w)
                if not parsed:
                    continue
                open_t, close_t = parsed
                open_min  = open_t.hour * 60 + open_t.minute
                close_min = close_t.hour * 60 + close_t.minute
                if open_min - grace_before <= t_min < open_min:
                    return True
                if close_min <= t_min < close_min + grace_after:
                    return True
            return False
        except Exception:
            return True

    @staticmethod
    def _within_actual_close_grace(ctx: DetectorContext) -> bool:
        """True during routine egress after an observed shop closure."""
        if ctx.store_id is None:
            return False
        try:
            import redis

            from app.config import settings as _s
            from app.utils.business_hours import (
                ACTUAL_CLOSE_KEY_FMT,
                actual_close_grace_active,
            )

            marker = redis.from_url(
                _s.redis_url, decode_responses=True,
            ).get(ACTUAL_CLOSE_KEY_FMT.format(store_id=int(ctx.store_id)))
            return actual_close_grace_active(
                marker,
                now_epoch=time.time(),
                grace_minutes=_s.person_afterhours_actual_close_grace_min,
            )
        except Exception:
            return False

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        # Determine whether the store is closed right now. Any camera
        # WITH a store assignment goes through business hours (with the
        # default-window fallback for unconfigured/malformed hours —
        # previously {} or a missing weekday key meant "closed", arming
        # intrusion at 10:55 EAT with the store wide open). always_armed
        # only applies to store-less cameras, per the class docstring.
        if ctx.store_id is not None and ctx.store_timezone:
            local_now = localised_now(ctx.store_timezone)
            closed = not is_open_with_default(ctx.business_hours, local_now)
            # before_hours (earlier than today's opening) vs after_hours
            # (past closing) — drives the alert wording so a 07:30
            # staff arrival reads differently from a 21:30 presence.
            time_context = intrusion_time_context(ctx.business_hours, local_now)
        else:
            closed = bool((cfg.get("extra") or {}).get("always_armed"))
            time_context = "after_hours"
        if not closed:
            return []
        # An observed closure is stronger evidence than the scheduled close.
        # Do not escalate routine staff egress until its 30-minute allowance
        # has elapsed; a Redis failure deliberately leaves alerts enabled.
        if self._within_actual_close_grace(ctx):
            return []

        thr = float(cfg.get("confidence_threshold", 0.5))
        zones = [z for z in ctx.zones if "restricted" in (z.get("detection_types_json") or [])]
        if not zones:
            # No restricted zones defined → treat the entire frame as armed.
            zones = [{"id": None, "polygon_coords_json": [[0, 0], [1, 0], [1, 1], [0, 1]],
                      "detection_types_json": ["restricted"], "suppressed": False}]

        out: list[DetectionEvent] = []
        now = time.time()
        extra = cfg.get("extra") or {}
        rearm_seconds = max(0.0, float(
            extra.get("incident_rearm_seconds", 300.0)))
        fingerprint_bucket = max(0.005, float(
            extra.get("incident_fingerprint_bucket", 0.02)))
        # Grace window — minutes before opening / after closing during
        # which a person identified as staff (medium/high) is treated
        # as opening / closing the store rather than an intruder. Same
        # default (60 min) on both sides via existing settings.
        in_grace = self._within_after_hours_grace(ctx)
        from app.ai.detectors import staff_identity
        active_keys: set[tuple[int, int, int]] = set()
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON or det["conf"] < thr:
                continue
            for z in zones:
                if bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"]):
                    tid = staff_identity.match_track(ctx, det)
                    verdict = staff_identity.classify(ctx, det, tid, now)
                    level = verdict["level"]
                    # Tier the response:
                    # • staff (high/medium) IN grace → fully suppress
                    #   (this IS the opening/closing person)
                    # • uncertain pixels → always suppress; rather miss
                    #   an intruder ping than alarm on staff the camera
                    #   can't read clearly
                    # • staff OUT of grace → ATTENTION (staff present
                    #   but at an unusual time)
                    # • unknown → URGENT (original behaviour)
                    if level in ("high", "medium") and in_grace:
                        staff_identity.mark_staff_track(
                            ctx, tid, source="opening_closing")
                        break
                    if level == "uncertain":
                        break

                    zid = z.get("id") or -1
                    incident_key = (ctx.camera_id, int(tid), int(zid))
                    active_keys.add(incident_key)
                    self._last_seen[incident_key] = now
                    persistent_allowed = self._persistent_incident_allowed(
                        ctx, int(zid), det["bbox_norm"], rearm_seconds,
                        fingerprint_bucket,
                    )
                    if incident_key in self._fired:
                        continue
                    self._fired.add(incident_key)
                    if not persistent_allowed:
                        continue
                    if level in ("high", "medium"):
                        # Staff outside the grace window — downgrade so
                        # the manager sees it without being WhatsApped
                        # in the middle of the night.
                        staff_identity.mark_staff_track(
                            ctx, tid, source="afterhours_present")
                        priority = "attention"
                        reason = "staff_present_after_hours"
                    else:
                        priority = "high"
                        reason = "after_hours"
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls="person",
                        confidence=det["conf"], bbox_norm=det["bbox_norm"],
                        track_id=int(tid), zone_id=z.get("id"),
                        extra={"priority": priority, "store_id": ctx.store_id,
                               # after_hours stays True for backward
                               # compat; time_context carries the
                               # before/after distinction.
                               "after_hours": True,
                               "time_context": time_context,
                               "staff_level": level,
                               "rule": reason,
                               "incident_rearm_seconds": rearm_seconds},
                    ))
                    break

        # Re-arm only after a genuine absence.  A short tracker miss or
        # occlusion must not recreate the same incident.
        for key, last_seen in list(self._last_seen.items()):
            if key in active_keys:
                continue
            if now - last_seen >= rearm_seconds:
                self._last_seen.pop(key, None)
                self._fired.discard(key)
        return out
