"""Priority-2 retail detectors:

  - StaffPresenceDetector  — % of time a 'counter'-tagged zone is staffed
  - AisleDwellDetector     — avg dwell time per aisle zone, ranked
  - ShutterDetector        — shutter open/closed via custom class label or
                             a fallback brightness heuristic on a zone
  (HeatmapDetector already exists — P2 adds an export endpoint, see
   `app.api.analytics.heatmap_image`.)
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from statistics import mean

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.detectors import staff_identity
from app.ai.detectors.uniform_compliance import uniform_features
from app.ai.zone_logic import bbox_in_zone


# ---------------------------------------------------------------------------
# Staff availability at a counter
# ---------------------------------------------------------------------------

class StaffPresenceDetector(Detector):
    """Multi-evidence "counter attended" detector.

    REFACTOR (Jun 2026): the old "any person in counter zone =
    attended" rule produced both false alerts (cashier briefly
    occluded by a tall customer → 5-min countdown started) and
    false-negative cover (a lingering customer kept the counter
    "attended" all day). The new model is a confidence score:

      For every tracked person inside the `counter` polygon OR an
      adjacent `staff_zone` (staff-behind-counter), compute:
          uniform colour      40 / 20 / 0   (top_ok / partial / none)
          staff identity      30 / 20 / 10 / 0 (high / med / uncertain / unknown)
          persistent tracking 1 pt / 15 s in zone, capped at 20
          zone occupancy      10  (floor — any person in zone)
      Sum → 0..100 per person; counter score = MAX across all people
      in either zone, including staff seated behind the counter.

      Counter is ATTENDED when score ≥ 60.
      Counter is UNATTENDED when score < 40.
      40-60 keeps the previous state (hysteresis) so a single sub-40
      frame from an occluded cashier doesn't flip everything.

    Alert (URGENT) fires only when:
      • score has been < 40 CONTINUOUSLY for UNATTENDED_DWELL_SECONDS
        (5 min), AND
      • we are past the ABSENCE_GRACE_SECONDS (60 s) following the
        last attended frame — covers short occlusions / dropped
        detections / camera blink, AND
      • the store is currently inside business hours, AND
      • we are outside the opening / closing grace bands, AND
      • per-zone Redis dedupe says we haven't fired in the last
        DEDUP_SECONDS (30 min).

    Persists per-zone state in Redis (`vg:counter_attendance:{cam}:
    {zone}`) so a worker restart doesn't reset the 5-min countdown.
    The detection_type, the alert cls, the metric name, and the
    extra payload shape all stay backward-compatible — every
    downstream alert / analytics surface keeps working.
    """

    detection_type = "staff_present"
    needs_tracking = True

    # Score thresholds (hysteresis band 40–60).
    SCORE_ATTENDED   = 60
    SCORE_UNATTENDED = 40

    # Alert timing.
    UNATTENDED_DWELL_SECONDS = 10 * 60      # must be unattended this long (10 min)
    ABSENCE_GRACE_SECONDS    = 120          # 2-min grace after last person seen
                                            # at the counter before the countdown
                                            # effectively starts (occlusions,
                                            # dropped detections, brief step-away)
    DEDUP_SECONDS            = 30 * 60      # 30 min between URGENT repeats

    OPENING_GRACE_SECONDS = 30 * 60
    CLOSING_GRACE_SECONDS = 15 * 60
    SERVICE_TIME_ALERT_SECONDS = 5 * 60
    SERVICE_TIME_MIN_SECONDS   = 30

    # Redis state key.
    _STATE_KEY_FMT = "vg:counter_attendance:{camera_id}:{zone_id}"
    _STATE_TTL_SECONDS = 3600

    def __init__(self):
        # (track_id, zone_id) → entry epoch — drives the service-time metric.
        self._counter_entries: dict[tuple[int, int], float] = {}
        # (camera_id, zone_id) → state dict {score, attended,
        #   below_40_since, last_attended_at, last_alert_at}.
        # In-memory authoritative; Redis is a warm-restart cache.
        self._state: dict[tuple[int, int], dict] = {}

    # ---- pure scoring helpers (unit-tested) ------------------------

    @staticmethod
    def score_uniform(feats: dict | None) -> int:
        """40 / 20 / 0 — uniform colour evidence."""
        if not feats:
            return 0
        if feats.get("top_ok"):
            return 40
        if float(feats.get("top_share") or 0) >= 0.10:
            return 20
        return 0

    @staticmethod
    def score_identity(level: str | None) -> int:
        """30 / 20 / 10 / 0 — staff_identity classify().level."""
        return {"high": 30, "medium": 20, "uncertain": 10}.get(level or "", 0)

    @staticmethod
    def score_dwell(time_in_zone_s: float) -> int:
        """1 pt per 15 s of presence in counter/staff_zone, capped at 20.
        Reaches the cap after 5 min (matches the time-based staff rule
        in staff_identity)."""
        if not time_in_zone_s or time_in_zone_s <= 0:
            return 0
        return int(min(20.0, time_in_zone_s / 15.0))

    @classmethod
    def person_score(cls, feats: dict | None, level: str | None,
                     time_in_zone_s: float) -> int:
        """Sum per-person evidence components; always include the
        +10 zone-occupancy floor (caller only invokes this when the
        person is in a counter / staff_zone polygon)."""
        return (cls.score_uniform(feats)
                + cls.score_identity(level)
                + cls.score_dwell(time_in_zone_s)
                + 10)

    @classmethod
    def apply_hysteresis(cls, score: int,
                          previously_attended: bool) -> bool:
        """≥60 attended; <40 unattended; the 40-60 band keeps the
        previous state so single-frame jitter doesn't flip."""
        if score >= cls.SCORE_ATTENDED:
            return True
        if score < cls.SCORE_UNATTENDED:
            return False
        return previously_attended

    # ---- Redis state plumbing --------------------------------------

    def _state_key(self, camera_id: int, zone_id: int) -> str:
        return self._STATE_KEY_FMT.format(camera_id=camera_id, zone_id=zone_id)

    def _load_state(self, camera_id: int, zone_id: int) -> dict:
        cache_key = (camera_id, zone_id)
        if cache_key in self._state:
            return self._state[cache_key]
        # Cold start — try Redis (warm-restart recovery).
        loaded: dict = {
            "score":            0,
            "attended":         False,
            "below_40_since":   None,
            "last_attended_at": None,
            "last_alert_at":    0.0,
        }
        try:
            import json
            import redis
            from app.config import settings as _settings
            r = redis.from_url(_settings.redis_url, decode_responses=True)
            raw = r.get(self._state_key(camera_id, zone_id))
            if raw:
                disk = json.loads(raw)
                loaded.update({k: disk.get(k, loaded[k]) for k in loaded})
        except Exception:
            pass
        self._state[cache_key] = loaded
        return loaded

    def _save_state(self, camera_id: int, zone_id: int, state: dict) -> None:
        try:
            import json
            import redis
            from app.config import settings as _settings
            r = redis.from_url(_settings.redis_url, decode_responses=True)
            r.set(self._state_key(camera_id, zone_id),
                  json.dumps(state), ex=self._STATE_TTL_SECONDS)
        except Exception:
            pass

    # ---- main --------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr_conf = float(cfg.get("confidence_threshold", 0.6))
        # `dwell_time_seconds` retained as a per-store override of the
        # 5-min unattended window — operators tune via detector_config.
        dwell    = int(cfg.get("dwell_time_seconds") or self.UNATTENDED_DWELL_SECONDS)

        counter_zones = [z for z in ctx.zones
                         if "counter" in (z.get("detection_types_json") or [])
                         and not z.get("suppressed")]
        if not counter_zones:
            return []
        # Adjacent staff_zone polygons on the same camera — a cashier
        # seated behind the till is normally in the staff_zone, not in
        # the customer-facing counter polygon.
        staff_zones = [z for z in ctx.zones
                       if "staff_zone" in (z.get("detection_types_json") or [])
                       and not z.get("suppressed")]

        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        out: list[DetectionEvent] = []
        now = time.time()

        # Closed-store suppression.
        if ctx.business_hours and ctx.store_timezone:
            try:
                from app.utils.business_hours import is_open, localised_now
                if not is_open(ctx.business_hours, localised_now(ctx.store_timezone)):
                    return []
            except Exception:
                pass
        in_opening_grace, in_closing_grace = self._opening_closing_grace(ctx, now)

        # Per-track presence at each counter zone — used for service
        # time + dwell scoring. Also feeds the staff_identity registry
        # so the classify() call below sees a meaningful dwell.
        active_track_zones: set[tuple[int, int]] = set()
        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in counter_zones:
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    key = (tr.track_id, z["id"])
                    active_track_zones.add(key)
                    self._counter_entries.setdefault(key, now)
                    staff_identity.observe(ctx.camera_id, tr.track_id,
                                           "counter", now)
            for z in staff_zones:
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    staff_identity.observe(ctx.camera_id, tr.track_id,
                                           "staff_zone", now)

        # Tracks that LEFT a counter zone — close the timer + emit
        # the service_time metric.
        for key in list(self._counter_entries.keys()):
            if key in active_track_zones:
                continue
            t0 = self._counter_entries.pop(key)
            duration = max(0.0, now - t0)
            if duration < self.SERVICE_TIME_MIN_SECONDS:
                continue
            zone_id = key[1]
            if ctx.db is not None:
                from app.analytics import recorder
                recorder.record(ctx.db, "service_time", float(duration),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zone_id, aggregator="avg")
            if duration >= self.SERVICE_TIME_ALERT_SECONDS:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="long_service",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1], zone_id=zone_id,
                    extra={"priority": "info", "store_id": ctx.store_id,
                           "service_seconds": int(duration),
                           "service_minutes": int(duration / 60),
                           "rule": "long_service"},
                ))

        # ---- per-counter attendance scoring -------------------------
        for z in counter_zones:
            # Collect candidate persons: tracked people in this counter
            # zone OR in any staff_zone on the same camera.
            candidates: list[tuple] = []  # (track_id, bbox_norm, dwell_s)
            for tr, _det in ctx.tracks:
                if tr.cls not in COCO_PERSON:
                    continue
                in_counter = bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"])
                in_staff = any(bbox_in_zone(tr.bbox_norm, sz["polygon_coords_json"])
                               for sz in staff_zones)
                if not (in_counter or in_staff):
                    continue
                # Dwell = MAX across counter + staff_zone (the cashier
                # may have walked between the two).
                d_counter = staff_identity.time_in_zone(
                    ctx.camera_id, tr.track_id, "counter", now)
                d_staff = staff_identity.time_in_zone(
                    ctx.camera_id, tr.track_id, "staff_zone", now)
                candidates.append((tr.track_id, tr.bbox_norm,
                                   max(d_counter, d_staff)))

            # Score the best candidate; zero when nobody is in either
            # zone right now.
            score = 0
            for track_id, bbox, dwell_s in candidates:
                feats = uniform_features(ctx.frame_bgr, bbox)
                verdict = staff_identity.classify(
                    ctx, {"bbox_norm": bbox}, track_id, now)
                s = self.person_score(feats, verdict.get("level"), dwell_s)
                if s > score:
                    score = s

            state = self._load_state(ctx.camera_id, z["id"])
            prev_attended = bool(state.get("attended"))
            attended = self.apply_hysteresis(score, prev_attended)
            # Presence override: ANY person physically inside the counter
            # polygon means the counter is attended — a customer being served
            # or a non-uniform staffer still means someone is there. This
            # prevents false "Counter Left Unattended" alerts when the person
            # simply isn't uniform-classified (low score but clearly present).
            person_in_counter = any(zid == z["id"]
                                    for (_tid, zid) in active_track_zones)
            if person_in_counter:
                attended = True
            state["score"] = score
            state["attended"] = attended

            if attended:
                state["last_attended_at"] = now
                state["below_40_since"]   = None
            else:
                # Mark below-40 transition exactly once.
                if score < self.SCORE_UNATTENDED and state["below_40_since"] is None:
                    state["below_40_since"] = now

            self._save_state(ctx.camera_id, z["id"], state)

            # Always-on metric — kept at the existing key for
            # backwards compat with the dashboards.
            if ctx.db is not None:
                from app.analytics import recorder
                recorder.record(ctx.db, "staff_present_pct",
                                1.0 if attended else 0.0,
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=z["id"], aggregator="last")

            # ---- alert ---------------------------------------------
            if attended:
                continue
            if in_opening_grace or in_closing_grace:
                continue
            # No point alerting that the counter is unstaffed before the
            # store opens or after it closes — staff aren't expected. The
            # grace check above only covers the ±minutes around open/close;
            # this suppresses the whole closed period (e.g. 07:37 EAT).
            if self._store_closed_now(ctx):
                continue
            below_since = state.get("below_40_since")
            if below_since is None:
                continue
            # 60 s absence grace — start the 5-min countdown from
            # max(below_since, last_attended_at + grace).
            last_att = state.get("last_attended_at") or 0.0
            effective_since = max(below_since, last_att + self.ABSENCE_GRACE_SECONDS)
            continuous_below = now - effective_since
            if continuous_below < dwell:
                continue
            if (now - (state.get("last_alert_at") or 0.0)) <= self.DEDUP_SECONDS:
                continue

            # Fire.
            state["last_alert_at"] = now
            self._save_state(ctx.camera_id, z["id"], state)
            boxes: list[dict] = []
            poly = z.get("polygon_coords_json") or []
            if poly:
                xs = [pt[0] for pt in poly]; ys = [pt[1] for pt in poly]
                boxes.append({"bbox": [min(xs), min(ys), max(xs), max(ys)],
                              "color": "red", "label": "Counter unattended"})
            out.append(DetectionEvent(
                detection_type=self.detection_type, cls="counter_unstaffed",
                confidence=1.0, bbox_norm=[0, 0, 1, 1], zone_id=z["id"],
                extra={
                    "unstaffed_seconds": int(continuous_below),
                    "unstaffed_minutes": int(continuous_below / 60),
                    "store_id":          ctx.store_id,
                    "boxes":             boxes,
                    "attendance_score":  int(score),
                    "rule":              "counter_unattended",
                },
            ))
        return out

    def _opening_closing_grace(self, ctx: DetectorContext,
                               now: float) -> tuple[bool, bool]:
        """(within_30min_of_open, within_15min_of_close) for today,
        in the store's local timezone. (False, False) when hours
        aren't available."""
        if not ctx.business_hours or not ctx.store_timezone:
            return False, False
        try:
            from app.utils.business_hours import localised_now
            now_local = localised_now(ctx.store_timezone)
            weekday_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            windows = ctx.business_hours.get(weekday_keys[now_local.weekday()]) or []
            opens, closes = [], []
            from datetime import datetime as _dt
            for win in windows:
                try:
                    a, b = win.split("-")
                    ha, ma = (int(x) for x in a.split(":"))
                    hb, mb = (int(x) for x in b.split(":"))
                    opens.append(_dt(now_local.year, now_local.month, now_local.day,
                                     ha, ma, tzinfo=now_local.tzinfo))
                    closes.append(_dt(now_local.year, now_local.month, now_local.day,
                                      hb, mb, tzinfo=now_local.tzinfo))
                except Exception:
                    continue
            if not opens:
                return False, False
            earliest_open = min(opens)
            latest_close = max(closes)
            opening = 0 <= (now_local - earliest_open).total_seconds() <= self.OPENING_GRACE_SECONDS
            closing = 0 <= (latest_close - now_local).total_seconds() <= self.CLOSING_GRACE_SECONDS
            return opening, closing
        except Exception:
            return False, False

    def _store_closed_now(self, ctx: DetectorContext) -> bool:
        """True when the store is currently CLOSED, so a counter-unstaffed
        alert (expected overnight / before opening) is suppressed.

        Unlike `_opening_closing_grace`, which only covers the ±minutes
        around open/close, this covers the ENTIRE closed period. Uses the
        store's configured business hours when available, else a default
        08:00-21:00 EAT window. If the timezone can't be resolved it
        treats the store as open (returns False) so a config error never
        silently silences alerts."""
        tz = ctx.store_timezone or "Africa/Nairobi"
        try:
            from app.utils.business_hours import is_open, localised_now
            now_local = localised_now(tz)
        except Exception:
            return False
        if ctx.business_hours:
            return not is_open(ctx.business_hours, now_local)
        # No configured hours → default EAT operating window.
        return not (8 <= now_local.hour < 21)


# ---------------------------------------------------------------------------
# Aisle dwell time
# ---------------------------------------------------------------------------

class AisleDwellDetector(Detector):
    """Browse-time + sales-floor coverage detector for `aisle` /
    `dwell` zones (Product Interest Intelligence + Sales Floor
    Coverage panels in the dashboard).

    Per-track per-zone:
      - Entry timestamp; on exit emit a per-track dwell event when
        the visit was ≥ MIN_BROWSE_SECONDS (pass-throughs ignored).
      - Visit count — every fresh entry to a zone in the same
        session bumps the per-(track, zone) counter for the
        repeat-visit metric.

    Per-zone, every frame batch:
      - Staff vs customer split via app.ai.detectors.staff_identity
        (uniform colour + ≥5 min dwell). Staff in an aisle counts
        toward floor coverage; their dwell does NOT count as
        browsing.
      - dwell_seconds (avg of in-progress dwells)         — rolling avg
      - browse_time_seconds (alias)                        — rolling avg
      - dwell_count (customers ≥3 s in zone right now)    — last
      - dwell_repeat_rate (% with visits > 1, this period) — last
      - staff_present (1 when ≥1 staff in zone, else 0)   — last
      - staff_customer_ratio (staff / max(1, customers))  — last
      - aisle_unattended_seconds (continuous run, 0 when staffed
        or below threshold)                              — last

    Alerts:
      sales_floor_unattended — ≥UNATTENDED_CUSTOMERS customers in
                               the zone, NO staff, for ≥UNATTENDED_
                               SECONDS. Dedup once per UNATTENDED_
                               DEDUP_SECONDS. Suppressed in the
                               first / last 30 min of trading.

    Heatmap integration (writes per evaluate, debounced):
      vg:heatmap:dwell:{camera_id}
        { zones: { zone_id: { avg_seconds, count, hot_spot,
                              repeat_rate } }, updated_at }
      The traffic + engagement heatmaps come from HeatmapDetector;
      this key is the dedicated dwell-time toggle layer.
    """

    detection_type = "dwell"
    needs_tracking = True

    # ─ Sustained-duration knobs ────────────────────────────────────
    # Quick pass-throughs are filtered out — only browses ≥3 s count.
    MIN_BROWSE_SECONDS  = 3.0
    UNATTENDED_CUSTOMERS = 8         # ≥N customers + no staff = alert
    UNATTENDED_MIN_CUSTOMERS = 5     # below this, never alert
    UNATTENDED_SECONDS   = 3 * 60    # sustained gap before alert
    UNATTENDED_DEDUP_SECONDS = 10 * 60
    STAFF_LEFT_GRACE_SECONDS = 2 * 60   # staff just left → don't alert
    # Repeat-visit clock — bumping the counter for the same zone
    # within REVISIT_DEBOUNCE_SECONDS is treated as the same visit
    # (the tracker briefly lost the person between shelves).
    REVISIT_DEBOUNCE_SECONDS = 30
    HEATMAP_PUBLISH_SECONDS = 30
    OPENING_GRACE_SECONDS = 30 * 60
    CLOSING_GRACE_SECONDS = 30 * 60

    def __init__(self):
        # (camera, track, zone) → entry timestamp
        self._entered: dict[tuple[int, int, int], float] = {}
        # (camera, track, zone) → visit_count (resets when track ages out)
        self._visits: dict[tuple[int, int, int], int] = {}
        # (camera, track, zone) → last exit timestamp (for revisit debounce)
        self._last_exit: dict[tuple[int, int, int], float] = {}
        # (camera, zone) → epoch sales-floor-unattended started
        self._unattended_since: dict[tuple[int, int], float] = {}
        # (camera, zone) → last alert epoch
        self._unattended_fired: dict[tuple[int, int], float] = {}
        # (camera, zone) → last staff-seen epoch (drives the grace)
        self._last_staff_seen: dict[tuple[int, int], float] = {}
        # camera_id → last heatmap publish ts
        self._last_heatmap_publish: dict[int, float] = {}

    # -----------------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        # 'aisle' is the canonical tag; 'dwell' is the legacy alias —
        # accept both so older zone configs keep feeding metrics.
        zones = [z for z in ctx.zones
                 if {"aisle", "dwell"} & set(z.get("detection_types_json") or [])]
        if not zones:
            return []

        out: list[DetectionEvent] = []
        now = time.time()
        in_opening_grace, in_closing_grace = self._opening_closing_grace(ctx, now)

        active_track_ids = {tr.track_id for tr, _ in ctx.tracks
                            if tr.cls in COCO_PERSON}

        # Per-zone in-progress dwells + visit-count snapshot for this
        # frame batch. We classify each track as staff/customer once
        # via staff_identity so the per-zone counts stay consistent.
        per_zone_dwells:    dict[int, list[float]]   = {z["id"]: [] for z in zones}
        per_zone_customers: dict[int, set[int]]      = {z["id"]: set() for z in zones}
        per_zone_staff:     dict[int, set[int]]      = {z["id"]: set() for z in zones}
        per_zone_revisits:  dict[int, list[int]]     = {z["id"]: [] for z in zones}

        # Walk tracker frames first — pick up entries/exits and update
        # the per-track per-zone bookkeeping.
        for tr, det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            # Staff classification is per track, not per zone — a single
            # call decides whether the person should count as staff
            # everywhere they appear in this frame.
            verdict = staff_identity.classify(
                ctx, {"bbox_norm": tr.bbox_norm}, tr.track_id, now)
            is_staff = verdict["level"] in ("high", "medium")

            for z in zones:
                key = (ctx.camera_id, tr.track_id, z["id"])
                in_zone = bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"])
                if in_zone:
                    # Track is currently in the zone. First sighting in
                    # this visit increments the per-zone visit counter.
                    if key not in self._entered:
                        last_exit = self._last_exit.get(key, 0.0)
                        if now - last_exit > self.REVISIT_DEBOUNCE_SECONDS:
                            self._visits[key] = self._visits.get(key, 0) + 1
                        self._entered[key] = now
                    elapsed = max(0.0, now - self._entered[key])
                    if is_staff:
                        per_zone_staff[z["id"]].add(tr.track_id)
                        self._last_staff_seen[(ctx.camera_id, z["id"])] = now
                    elif elapsed >= self.MIN_BROWSE_SECONDS:
                        per_zone_customers[z["id"]].add(tr.track_id)
                        per_zone_dwells[z["id"]].append(elapsed)
                        per_zone_revisits[z["id"]].append(self._visits.get(key, 1))
                else:
                    # Track left this zone — emit the dwell event for
                    # customers (staff dwells don't model browsing).
                    if key in self._entered:
                        dwell = max(0.0, now - self._entered.pop(key))
                        self._last_exit[key] = now
                        if not is_staff and dwell >= self.MIN_BROWSE_SECONDS:
                            out.append(DetectionEvent(
                                detection_type=self.detection_type,
                                cls="aisle_dwell",
                                confidence=1.0,
                                bbox_norm=tr.bbox_norm,
                                track_id=tr.track_id, zone_id=z["id"],
                                extra={
                                    "dwell_seconds": round(dwell, 1),
                                    "browse_time_seconds": round(dwell, 1),
                                    "visit_count": self._visits.get(key, 1),
                                    "store_id": ctx.store_id,
                                },
                            ))

        # Reap entries / counters for tracks that have fully aged out.
        for k in list(self._entered):
            if k[1] not in active_track_ids:
                self._entered.pop(k, None)
        for k in list(self._visits):
            if k[1] not in active_track_ids:
                self._visits.pop(k, None)
                self._last_exit.pop(k, None)

        # ── per-zone metrics + sales-floor-unattended logic ─────────
        if ctx.db is not None:
            from app.analytics import recorder
            for z in zones:
                zid = z["id"]
                dwells = per_zone_dwells[zid]
                customers = per_zone_customers[zid]
                staff = per_zone_staff[zid]
                revisits = per_zone_revisits[zid]
                avg_s = float(mean(dwells)) if dwells else 0.0
                repeat_rate = (
                    sum(1 for v in revisits if v > 1) / len(revisits)
                    if revisits else 0.0)
                staff_present = 1.0 if staff else 0.0
                staff_cust_ratio = (
                    len(staff) / max(1, len(customers)) if customers else 0.0)

                # Legacy + new metric names (same value) so existing
                # dashboards keep rendering while the new UI rolls out.
                if dwells:
                    recorder.record(ctx.db, "dwell_seconds", avg_s,
                                    camera_id=ctx.camera_id,
                                    store_id=ctx.store_id,
                                    zone_id=zid, aggregator="avg")
                    recorder.record(ctx.db, "browse_time_seconds", avg_s,
                                    camera_id=ctx.camera_id,
                                    store_id=ctx.store_id,
                                    zone_id=zid, aggregator="avg")
                recorder.record(ctx.db, "dwell_count", float(len(customers)),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="last")
                recorder.record(ctx.db, "dwell_repeat_rate", float(repeat_rate),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="avg")
                recorder.record(ctx.db, "staff_present", staff_present,
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="last")
                recorder.record(ctx.db, "staff_customer_ratio",
                                float(staff_cust_ratio),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="avg")

                # ── sales-floor-unattended ────────────────────────
                gap_key = (ctx.camera_id, zid)
                staff_recently = (
                    now - self._last_staff_seen.get(gap_key, 0.0)
                    < self.STAFF_LEFT_GRACE_SECONDS)
                qualifies = (len(customers) >= self.UNATTENDED_CUSTOMERS
                             and not staff
                             and not staff_recently
                             and len(customers) >= self.UNATTENDED_MIN_CUSTOMERS)
                if qualifies:
                    self._unattended_since.setdefault(gap_key, now)
                else:
                    self._unattended_since.pop(gap_key, None)

                unattended_for = (
                    now - self._unattended_since[gap_key]
                    if gap_key in self._unattended_since else 0.0)
                recorder.record(ctx.db, "aisle_unattended_seconds",
                                float(unattended_for),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="last")

                if (qualifies
                        and not in_opening_grace and not in_closing_grace
                        and unattended_for >= self.UNATTENDED_SECONDS
                        and now - self._unattended_fired.get(gap_key, 0.0)
                                >= self.UNATTENDED_DEDUP_SECONDS):
                    self._unattended_fired[gap_key] = now
                    boxes: list[dict] = []
                    poly = z.get("polygon_coords_json") or []
                    if poly:
                        xs = [pt[0] for pt in poly]
                        ys = [pt[1] for pt in poly]
                        boxes.append({
                            "bbox": [min(xs), min(ys), max(xs), max(ys)],
                            "color": "amber",
                            "label": f"{len(customers)} customers, no staff",
                        })
                    out.append(DetectionEvent(
                        detection_type=self.detection_type,
                        cls="sales_floor_unattended",
                        confidence=1.0, bbox_norm=[0, 0, 1, 1], zone_id=zid,
                        extra={
                            "priority": "warning",
                            "store_id": ctx.store_id,
                            "rule": "sales_floor_unattended",
                            "customer_count": len(customers),
                            "unattended_seconds": int(unattended_for),
                            "unattended_minutes": int(unattended_for / 60),
                            "zone_name": z.get("name"),
                            "boxes": boxes,
                        },
                    ))

        # Publish the dwell heatmap layer at most every
        # HEATMAP_PUBLISH_SECONDS. Keeps Redis writes cheap on busy
        # cameras while still feeling live to the operator.
        if (now - self._last_heatmap_publish.get(ctx.camera_id, 0)
                >= self.HEATMAP_PUBLISH_SECONDS):
            self._last_heatmap_publish[ctx.camera_id] = now
            self._publish_dwell_heatmap(ctx, zones, per_zone_dwells,
                                        per_zone_customers, per_zone_revisits,
                                        now)
        return out

    # -----------------------------------------------------------------

    def _publish_dwell_heatmap(self, ctx: DetectorContext, zones: list[dict],
                               per_zone_dwells: dict[int, list[float]],
                               per_zone_customers: dict[int, set[int]],
                               per_zone_revisits: dict[int, list[int]],
                               now: float) -> None:
        """Write per-zone dwell aggregates to
        vg:heatmap:dwell:{camera_id}. The map UI uses this for the
        "Dwell Time View" toggle — red = highest avg, blue = quick
        pass-through."""
        try:
            import json
            import redis
            from app.config import settings
            r = redis.from_url(settings.redis_url)
            payload = {"camera_id": ctx.camera_id,
                       "updated_at": now,
                       "zones": {}}
            for z in zones:
                zid = z["id"]
                dwells = per_zone_dwells.get(zid, [])
                customers = per_zone_customers.get(zid, set())
                revisits = per_zone_revisits.get(zid, [])
                avg_s = float(mean(dwells)) if dwells else 0.0
                payload["zones"][str(zid)] = {
                    "zone_name": z.get("name"),
                    "polygon":   z.get("polygon_coords_json") or [],
                    "avg_seconds": round(avg_s, 1),
                    "count":       len(customers),
                    "repeat_rate": (
                        round(sum(1 for v in revisits if v > 1)
                              / len(revisits), 3)
                        if revisits else 0.0),
                }
            r.set(f"vg:heatmap:dwell:{ctx.camera_id}",
                  json.dumps(payload), ex=24 * 3600)
        except Exception:
            pass

    # -----------------------------------------------------------------

    def _opening_closing_grace(self, ctx: DetectorContext,
                               now: float) -> tuple[bool, bool]:
        if not ctx.business_hours or not ctx.store_timezone:
            return False, False
        try:
            from app.utils.business_hours import localised_now
            from datetime import datetime as _dt
            now_local = localised_now(ctx.store_timezone)
            weekday_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            windows = ctx.business_hours.get(weekday_keys[now_local.weekday()]) or []
            opens, closes = [], []
            for win in windows:
                try:
                    a, b = win.split("-")
                    ha, ma = (int(x) for x in a.split(":"))
                    hb, mb = (int(x) for x in b.split(":"))
                    opens.append(_dt(now_local.year, now_local.month,
                                     now_local.day, ha, ma,
                                     tzinfo=now_local.tzinfo))
                    closes.append(_dt(now_local.year, now_local.month,
                                      now_local.day, hb, mb,
                                      tzinfo=now_local.tzinfo))
                except Exception:
                    continue
            if not opens:
                return False, False
            opening = 0 <= (now_local - min(opens)).total_seconds() <= self.OPENING_GRACE_SECONDS
            closing = 0 <= (max(closes) - now_local).total_seconds() <= self.CLOSING_GRACE_SECONDS
            return opening, closing
        except Exception:
            return False, False


# ---------------------------------------------------------------------------
# Shutter open/closed
# ---------------------------------------------------------------------------

class ShutterDetector(Detector):
    """Three-state shutter/door detector: OPEN / CLOSED / PARTIAL.

    Two modes:

    1. Custom-model mode (preferred): if the configured AI model emits
       class labels `shutter_open` / `shutter_closed` / `shutter_partial`
       (configurable via extra), the highest-confidence detection wins.

    2. Rule-based mode (works NOW, no training): combines two cheap
       image features measured inside the shutter zone polygon —

         • brightness — mean grayscale (0..255). A closed metal/glass
           shutter is much darker than the lit interior behind it.
         • texture    — variance of the Laplacian. A smooth shutter
           face has near-zero texture; a visible store interior
           (shelves, signage, people) has high texture.

       brightness + texture together separate the three states far
       more reliably than brightness alone, which is why operators
       were missing real opens/closes:

         dark  + smooth        → CLOSED  (metal shutter, lights off)
         bright + textured     → OPEN    (interior visible)
         mid brightness OR
         brightness/texture
         disagree              → PARTIAL (mid-roll, or ambiguous)

    Alert rules:
      • CLOSED during business hours          → "shutter closed during hours" (high)
      • OPEN outside business hours           → "shutter open after hours"     (high)
      • PARTIAL sustained > 5 min             → "shutter stuck partially open"  (high)
      • Still CLOSED within 30 min of opening → "store not yet open"            (high)
    Writes a `shutter_open` metric (1.0 open / 0.5 partial / 0.0 closed).
    """

    detection_type = "shutter"
    needs_tracking = False

    # State constants. Kept as ints so the hysteresis logic and the
    # `shutter_open` metric (open=1, partial=0.5, closed=0) stay simple.
    CLOSED, OPEN, PARTIAL = 0, 1, 2
    _STATE_NAME = {0: "closed", 1: "open", 2: "partial"}
    _STATE_METRIC = {0: 0.0, 1: 1.0, 2: 0.5}

    CONFIRM_FRAMES = 3
    # PARTIAL must persist this long before we raise the "stuck" alert.
    PARTIAL_STUCK_SECONDS = 5 * 60

    def __init__(self):
        # Committed (debounced) state per camera. 0/1/2.
        self._last_state: dict[int, int] = {}
        # Pending candidate flip — camera_id -> (candidate_state, run).
        self._pending: dict[int, tuple[int, int]] = {}
        self._fired: dict[tuple[int, str], float] = {}
        self._opening_alerted: dict[tuple[int, str, str], int] = {}
        # When the current PARTIAL run started, per camera (epoch).
        # Cleared whenever the committed state leaves PARTIAL.
        self._partial_since: dict[int, float] = {}

    def _detect_state(self, ctx: DetectorContext, zone: dict | None,
                      cfg: dict) -> int | None:
        """Best-guess state for THIS frame: OPEN / CLOSED / PARTIAL.
        Caller debounces via _commit_state(). Returns None only when
        no signal is available at all (no model class, no pixels)."""
        extra = cfg.get("extra") or {}
        # Mode 1: custom-class detections at/above the threshold win.
        cls_open    = extra.get("class_open",    "shutter_open")
        cls_closed  = extra.get("class_closed",  "shutter_closed")
        cls_partial = extra.get("class_partial", "shutter_partial")
        open_best    = max((d["conf"] for d in ctx.raw_detections if d["cls"] == cls_open),    default=0.0)
        closed_best  = max((d["conf"] for d in ctx.raw_detections if d["cls"] == cls_closed),  default=0.0)
        partial_best = max((d["conf"] for d in ctx.raw_detections if d["cls"] == cls_partial), default=0.0)
        thr = float(cfg.get("confidence_threshold", 0.5))
        if max(open_best, closed_best, partial_best) >= thr:
            best = max((open_best, self.OPEN),
                       (closed_best, self.CLOSED),
                       (partial_best, self.PARTIAL))
            return best[1]

        # Mode 2: brightness + texture rule-based classifier.
        # Gated behind settings.use_shutter_brightness so production
        # can run purely on the line-crossing path while keeping the
        # brightness code intact for A/B testing later. The HSV/
        # Laplacian path was unreliable on overhead cameras with the
        # in-store lighting drift we see at Vivo Junction.
        from app.config import settings as _settings
        if not _settings.use_shutter_brightness:
            return None
        if ctx.frame_bgr is None:
            return None
        feats = self._zone_features(ctx.frame_bgr,
                                    (zone or {}).get("polygon_coords_json"))
        if feats is None:
            return None
        brightness, texture = feats

        dark_thr     = float(extra.get("dark_threshold",     40))   # below → dark
        bright_thr   = float(extra.get("bright_threshold",   70))   # above → bright
        texture_low  = float(extra.get("texture_low",       100))   # below → smooth
        texture_high = float(extra.get("texture_high",      500))   # above → busy interior

        # Low-confidence model hints nudge brightness so a borderline
        # frame leans toward the model's vote (±10/255).
        if open_best > 0 or closed_best > 0:
            brightness += 10 * (open_best - closed_best)

        # Decisive brightness ends → trust it directly.
        if brightness <= dark_thr:
            # Dark AND smooth = definitely a closed shutter. Dark but
            # textured (rare: dim interior visible) is still closed —
            # the lights-off interior reads dark.
            return self.CLOSED
        if brightness >= bright_thr:
            # Bright. If the interior texture is also there it's a
            # confident OPEN; bright-but-smooth (e.g. a lit but blank
            # roller half-raised) is ambiguous → PARTIAL.
            return self.OPEN if texture >= texture_low else self.PARTIAL

        # Mid brightness — the old dead band. Use texture to break it
        # instead of returning None: a visible interior means OPEN, a
        # smooth face means CLOSED, anything between is a real PARTIAL.
        if texture >= texture_high:
            return self.OPEN
        if texture <= texture_low:
            return self.CLOSED
        return self.PARTIAL

    @staticmethod
    def _zone_features(frame_bgr, polygon_norm) -> tuple[float, float] | None:
        """(mean_brightness, laplacian_variance) inside the polygon.
        Brightness 0..255; texture is the variance of the Laplacian —
        a smooth shutter face is near 0, a busy store interior is high.
        Returns None if the polygon is invalid or cv2 is unavailable."""
        try:
            import numpy as np
            import cv2
        except ImportError:
            return None
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return None
        h, w = frame_bgr.shape[:2]
        if not polygon_norm or len(polygon_norm) < 3:
            # Require an explicit shutter polygon — a global crop fired
            # false closures on every dim camera at night.
            return None
        try:
            pts = np.array([[int(p[0] * w), int(p[1] * h)] for p in polygon_norm], dtype=np.int32)
            xs, ys = pts[:, 0], pts[:, 1]
            x0, x1 = max(0, xs.min()), min(w, xs.max())
            y0, y1 = max(0, ys.min()), min(h, ys.max())
            if x1 <= x0 or y1 <= y0:
                return None
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            # Mask brightness to the polygon; texture on the bbox crop
            # (Laplacian needs a dense rectangle, not a masked sparse set).
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(mask, [pts], 255)
            masked_pixels = gray[mask > 0]
            if masked_pixels.size == 0:
                return None
            brightness = float(masked_pixels.mean())
            crop = gray[y0:y1, x0:x1]
            texture = float(cv2.Laplacian(crop, cv2.CV_64F).var())
            return brightness, texture
        except Exception:
            return None

    def _commit_state(self, camera_id: int, observed: int) -> int:
        """Apply the consecutive-frames hysteresis. Returns the
        debounced committed state — same as the last one until
        CONFIRM_FRAMES samples in a row agree on the new value."""
        committed = self._last_state.get(camera_id)
        if committed is None:
            # Cold start — commit immediately on the first observation.
            self._last_state[camera_id] = observed
            self._pending.pop(camera_id, None)
            return observed
        if observed == committed:
            # Same as the committed state — clear any pending flip.
            self._pending.pop(camera_id, None)
            return committed
        # Different from committed — accumulate the pending run.
        candidate, run = self._pending.get(camera_id, (observed, 0))
        if candidate != observed:
            candidate, run = observed, 0
        run += 1
        if run >= self.CONFIRM_FRAMES:
            import logging as _l
            _l.getLogger(__name__).info(
                "Shutter transition: camera %s %s -> %s "
                "(confirmed after %d consecutive frames)",
                camera_id,
                self._STATE_NAME.get(committed, "?"),
                self._STATE_NAME.get(observed, "?"),
                run,
            )
            self._last_state[camera_id] = observed
            self._pending.pop(camera_id, None)
            return observed
        self._pending[camera_id] = (candidate, run)
        return committed

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        # Bug-fix: shutter previously fell back to "bottom third of frame"
        # when no zone was tagged — which fired on every dim-lit camera at
        # night. Now we REQUIRE an explicit zone tagged `shutter`.
        zone = None
        for z in ctx.zones:
            if "shutter" in (z.get("detection_types_json") or []):
                zone = z
                break
        if zone is None:
            return []

        observed = self._detect_state(ctx, zone, cfg)
        if observed is None:
            # Both signals too weak this frame — leave the committed
            # state where it is. Reusing the last committed value
            # keeps the metric write honest instead of skipping the
            # frame entirely, which previously made the dashboard
            # show 'shutter unknown' between solid reads.
            state = self._last_state.get(ctx.camera_id)
            if state is None:
                return []
        else:
            state = self._commit_state(ctx.camera_id, observed)

        # Publish the committed state to the shop-open/close cross-
        # detector module so EntryExitDetector can require shutter=open
        # before raising a "shop opened" alert on an inward crossing.
        try:
            from app.ai.detectors import shop_state as _shop
            _shop.set_shutter_state(ctx.camera_id,
                                    self._STATE_NAME.get(state, "closed"))
        except Exception:
            pass

        # Track how long we've been in PARTIAL so the "stuck" rule can
        # fire after PARTIAL_STUCK_SECONDS. Clear the timer whenever we
        # leave PARTIAL.
        now = time.time()
        if state == self.PARTIAL:
            self._partial_since.setdefault(ctx.camera_id, now)
        else:
            self._partial_since.pop(ctx.camera_id, None)

        # Metric — open=1.0, partial=0.5, closed=0.0.
        if ctx.db is not None:
            from app.analytics import recorder
            recorder.record(ctx.db, "shutter_open",
                            self._STATE_METRIC.get(state, 0.0),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            zone_id=(zone or {}).get("id"), aggregator="last")

        # Business-hours mismatch → alert.
        out: list[DetectionEvent] = []
        if ctx.business_hours is not None and ctx.store_timezone:
            from app.utils.business_hours import is_open, localised_now
            now_local = localised_now(ctx.store_timezone)
            is_open_now = is_open(ctx.business_hours, now_local)
            mismatch_open_after_hours  = (state == self.OPEN and not is_open_now)
            mismatch_closed_in_hours   = (state == self.CLOSED and is_open_now)
            if mismatch_open_after_hours and now - self._fired.get((ctx.camera_id, "open_after"), 0) > 300:
                self._fired[(ctx.camera_id, "open_after")] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="open_after_hours",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1],
                    zone_id=(zone or {}).get("id"),
                    extra={"priority": "high", "shutter_state": "open",
                           "store_id": ctx.store_id, "rule": "open_after_hours"},
                ))
            if mismatch_closed_in_hours and now - self._fired.get((ctx.camera_id, "closed_during"), 0) > 600:
                self._fired[(ctx.camera_id, "closed_during")] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="closed_during_hours",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1],
                    zone_id=(zone or {}).get("id"),
                    extra={"priority": "high", "shutter_state": "closed",
                           "store_id": ctx.store_id,
                           "rule": "closed_during_hours"},
                ))

            # "Shutter still closed at opening time" — once per day,
            # within 30 minutes of the first open window starting.
            # Distinct from the generic "closed during hours" rule so
            # the dashboard can flag late openings specifically.
            opening = self._first_opening_today(ctx.business_hours, now_local)
            if (state == self.CLOSED and opening is not None
                    and 0 <= (now_local - opening).total_seconds() <= 30 * 60):
                day_key = now_local.date().isoformat()
                if not self._opening_alerted.get((ctx.camera_id, "still_closed_at_opening", day_key)):
                    self._opening_alerted[(ctx.camera_id, "still_closed_at_opening", day_key)] = 1
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls="still_closed_at_opening",
                        confidence=1.0, bbox_norm=[0, 0, 1, 1],
                        zone_id=(zone or {}).get("id"),
                        extra={"priority": "high", "shutter_state": "closed",
                               "store_id": ctx.store_id,
                               "rule": "still_closed_at_opening",
                               "opening_time": opening.strftime("%H:%M")},
                    ))

        # "Stuck partially open/closed" — independent of business hours
        # because a half-rolled shutter is a fault any time of day
        # (jammed motor, obstruction). Fires once per sustained run,
        # re-arms only after the shutter leaves PARTIAL.
        partial_since = self._partial_since.get(ctx.camera_id)
        if (state == self.PARTIAL and partial_since is not None
                and now - partial_since >= self.PARTIAL_STUCK_SECONDS
                and now - self._fired.get((ctx.camera_id, "partial_stuck"), 0) > 600):
            self._fired[(ctx.camera_id, "partial_stuck")] = now
            mins = int((now - partial_since) / 60)
            out.append(DetectionEvent(
                detection_type=self.detection_type, cls="partial_stuck",
                confidence=1.0, bbox_norm=[0, 0, 1, 1],
                zone_id=(zone or {}).get("id"),
                extra={"priority": "high", "shutter_state": "partial",
                       "store_id": ctx.store_id, "rule": "partial_stuck",
                       "stuck_minutes": mins},
            ))
        return out

    @staticmethod
    def _first_opening_today(business_hours: dict | None, now_local):
        """Earliest open-window start on today's date in `now_local`'s
        timezone, or None if the store is closed today."""
        if not business_hours:
            return None
        from datetime import datetime as _dt
        weekday_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        windows = business_hours.get(weekday_keys[now_local.weekday()]) or []
        earliest = None
        for win in windows:
            try:
                a, _ = win.split("-")
                ha, ma = (int(x) for x in a.split(":"))
                start = _dt(now_local.year, now_local.month, now_local.day,
                            ha, ma, tzinfo=now_local.tzinfo)
                if earliest is None or start < earliest:
                    earliest = start
            except Exception:
                continue
        return earliest
