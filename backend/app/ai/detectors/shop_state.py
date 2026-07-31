"""Shop open / close alert state — bridges ShutterDetector and
EntryExitDetector so the "shop is open" decision needs BOTH signals
to agree.

The detectors stay independent — this module just holds the small
amount of cross-detector state they need to coordinate without
ordering assumptions.

Open detection (per spec):
  Two signals must agree on the same camera, same EAT day:
    (a) ShutterDetector reports the committed shutter state is OPEN.
    (b) EntryExitDetector observes an inward (direction='in')
        crossing of the entrance line.
  Only then does the camera count as "shop open" for the day.

Open alert bands (EAT, hard thresholds):
  before 09:00              shop_opened_before_hours    (URGENT)
  09:00 ≤ t ≤ 09:33         shop_opened                 (INFO)
  after 09:33               shop_opened_late            (ATTENTION)

Close detection (per spec):
  After 20:15 EAT, the FIRST outward crossing emits one
  shop_closed alert (INFO). First-crossing was chosen over last-
  crossing because the once-per-camera-per-day dedup makes "first
  seen" the predictable signal — there's no streaming way to know
  whether more staff will leave later, so waiting for the "last"
  would block the alert indefinitely. Subsequent outward crossings
  on the same day are silently ignored.

All four kinds are deduped once per (camera, EAT day, kind) via
Redis with a 36-hour TTL.

Thresholds live in the EntryExitDetector's `extra` config:
    trading_start_eat       (default "09:00")
    late_threshold_eat      (default "09:33")
    closing_threshold_eat   (default "20:15")
    shop_open_close_enabled (default True)

All time math runs in Africa/Nairobi via zoneinfo — never UTC.
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.ai.detectors.base import DetectorContext, DetectionEvent

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")

DEFAULT_TRADING_START   = "09:00"
DEFAULT_LATE_THRESHOLD  = "09:03"
DEFAULT_CLOSING_THRESH  = "20:00"
# Cut-off after which a still-not-opened store earns an URGENT
# "Store Not Opened" alert. Configurable via the entry_exit detector
# config's `extra.not_opened_cutoff_eat` field.
DEFAULT_NOT_OPENED_CUTOFF = "09:30"
# Outside this morning window, an inward line-crossing is NOT
# considered a store-opening signal. Before 07:00 the only people on
# camera are security / cleaning, so we ignore. After 09:30 every
# crossing is regular customer traffic — the metric still increments,
# but the open-alert path returns without firing.
EARLIEST_OPEN_EAT = "07:00"
# Symmetric upper bound on the close-detection window — outward
# crossings after this are just late customers leaving.
LATEST_CLOSE_EAT = "22:00"

# Detection type used for the alerts emitted from this module. NOT
# in app.ai.inference_worker._SKIP_ALERT_TYPES so events auto-promote
# to Alert rows.
SHOP_OPEN_CLOSE_TYPE = "shop_open_close"

# In-process cache of the latest committed shutter state per camera.
# ShutterDetector writes; EntryExitDetector reads. None means "we
# haven't seen this camera's shutter detector run yet" — usually
# because the camera has no shutter zone configured.
_committed_shutter: dict[int, str] = {}


def set_shutter_state(camera_id: int, state_name: str) -> None:
    """Called by ShutterDetector each frame after debouncing the
    OPEN/CLOSED/PARTIAL decision. state_name is one of
    'open' / 'closed' / 'partial'."""
    _committed_shutter[int(camera_id)] = state_name


def get_shutter_state(camera_id: int) -> str | None:
    return _committed_shutter.get(int(camera_id))


# ---------- Redis-backed dedupe ------------------------------------

def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _dedupe_key(camera_id: int, day_iso: str, kind: str) -> str:
    return f"vg:shop_open_close:fired:{camera_id}:{day_iso}:{kind}"


def _already_fired(camera_id: int, day_iso: str, kind: str) -> bool:
    try:
        r = _redis()
        return bool(r.get(_dedupe_key(camera_id, day_iso, kind)))
    except Exception:
        return False    # fail open — would rather alert twice than zero


def _mark_fired(camera_id: int, day_iso: str, kind: str) -> None:
    try:
        r = _redis()
        # 36 h TTL so the dedupe survives daylight-saving transitions
        # and the day rollover the moment the alert fires.
        r.set(_dedupe_key(camera_id, day_iso, kind), "1", ex=36 * 3600)
    except Exception:
        pass


# ---------- Store-level "opened today" coordination -----------------
# Single source of truth across BOTH the entrance-crossing path and
# the new occupancy-inference path (see alerting.shop_open_inference_check).
# The shop_not_opened_check task reads this to decide whether to fire
# the URGENT — so EITHER signal satisfies it.

# day_iso is the Africa/Nairobi (EAT) calendar date — NOT UTC — so the
# marker rolls over on the store's local midnight, avoiding boundary bugs.
_STORE_OPENED_KEY = "vg:store:opened:{store_id}:{day_iso}"


def store_opened_today(store_id: int, day_iso: str) -> dict | None:
    """Returns the opened-today marker for the store, or None if no
    open signal has been recorded yet today. Payload schema:
        { opened_at: ISO-8601 EAT,
          method:     "entry_crossing" | "occupancy_inference",
          confidence: "high" | "medium",
          camera_id:  int | None }
    Fails CLOSED on Redis errors — better to risk a duplicate alert
    than to silently drop one.
    """
    import json as _json
    try:
        r = _redis()
        raw = r.get(_STORE_OPENED_KEY.format(store_id=int(store_id),
                                              day_iso=day_iso))
        if not raw:
            return None
        try:
            return _json.loads(raw)
        except Exception:
            return None
    except Exception:
        return None


def release_store_opened(store_id: int, day_iso: str) -> None:
    """Delete the opened-today marker. Used when the alert write that
    FOLLOWED a successful claim failed — Redis and Postgres are not
    transactional together, so without this a post-claim DB error left
    the store 'opened' in Redis with no alert row, silencing the Store
    Opened alert for the whole day (20h TTL). Releasing lets the next
    crossing / beat tick claim again and retry."""
    try:
        _redis().delete(_STORE_OPENED_KEY.format(store_id=int(store_id),
                                                 day_iso=day_iso))
        log.warning("shop_state: released opened-today marker store=%s %s "
                    "(post-claim alert write failed — next signal retries)",
                    store_id, day_iso)
    except Exception as e:
        log.warning("shop_state: could not release opened marker store=%s: %s",
                    store_id, e)


def mark_store_opened(store_id: int, day_iso: str, *,
                     opened_at: datetime, method: str, confidence: str,
                     camera_id: int | None = None) -> bool:
    """Atomically claim the "opened today" slot for the store. Uses
    Redis SET NX so two concurrent writers can't race. Returns True
    when the caller actually claimed the slot (and therefore should
    fire its alert), False when a prior signal already claimed it.

    `opened_at` should already be in EAT (the canonical TZ for all
    store-open analytics)."""
    import json as _json
    payload = {
        "opened_at":  opened_at.isoformat(),
        "method":     method,
        "confidence": confidence,
        "camera_id":  camera_id,
        "store_id":   int(store_id),
    }
    try:
        r = _redis()
        key = _STORE_OPENED_KEY.format(store_id=int(store_id),
                                        day_iso=day_iso)
        # SET NX + 20h TTL: only the first writer of the day claims the
        # slot (returns True); the key expires well before the next
        # morning's opening window.
        return bool(r.set(key, _json.dumps(payload), ex=20 * 3600, nx=True))
    except Exception:
        # Fail OPEN — we'd rather risk a duplicate alert than silently
        # drop the only opening signal of the day.
        return True


# ---------- helpers ------------------------------------------------

def _now_eat() -> datetime:
    return datetime.now(timezone.utc).astimezone(EAT)


def _parse_hhmm(s: str) -> time | None:
    try:
        h, m = s.strip().split(":")
        return time(int(h), int(m))
    except Exception:
        return None


def _read_cfg(cfg_extra: dict | None) -> dict:
    extra = cfg_extra or {}
    return {
        "enabled":  bool(extra.get("shop_open_close_enabled", True)),
        "open_t":   _parse_hhmm(str(extra.get("trading_start_eat")
                                    or DEFAULT_TRADING_START)),
        "late_t":   _parse_hhmm(str(extra.get("late_threshold_eat")
                                    or DEFAULT_LATE_THRESHOLD)),
        "close_t":  _parse_hhmm(str(extra.get("closing_threshold_eat")
                                    or DEFAULT_CLOSING_THRESH)),
        "not_open_cutoff_t": _parse_hhmm(str(extra.get("not_opened_cutoff_eat")
                                             or DEFAULT_NOT_OPENED_CUTOFF)),
        "earliest_open_t":   _parse_hhmm(str(extra.get("earliest_open_eat")
                                             or EARLIEST_OPEN_EAT)),
        "latest_close_t":    _parse_hhmm(str(extra.get("latest_close_eat")
                                             or LATEST_CLOSE_EAT)),
    }


# ---------- open-alert decision ------------------------------------

def maybe_emit_open_alert(ctx: DetectorContext, cfg_extra: dict | None,
                           track_id: int, zone_id: int | None,
                           bbox_norm: list[float],
                           *, via_glass_door: bool = False) -> DetectionEvent | None:
    """Called from EntryExitDetector on every inward crossing. Returns
    a DetectionEvent to append to its output, or None.

    Requires BOTH:
      - ShutterDetector has committed state 'open' on this camera.
      - This is the first qualifying crossing today (per-camera-per-
        day dedupe across all three open bands).

    `via_glass_door=True` flags that the originating zone was tagged
    `glass_door` — the detector's 0.65-conf + 2-frame-persistence
    filter has already passed, so we surface that in the operator-
    facing message and the alert extra for downstream consumers
    (briefings, dashboards) that want to weight glass-door signals
    higher than the bare entry_exit kind.
    """
    cfg = _read_cfg(cfg_extra)
    if not cfg["enabled"]:
        return None
    if not (cfg["open_t"] and cfg["late_t"]):
        return None

    # Signal-agreement gate — only enforced when the brightness-based
    # shutter detector is enabled (settings.use_shutter_brightness).
    # With brightness disabled, ShutterDetector never publishes a
    # state, so requiring "open" here would silence every alert.
    from app.config import settings as _settings
    if _settings.use_shutter_brightness:
        if get_shutter_state(ctx.camera_id) != "open":
            return None

    now_eat = _now_eat()
    day_iso = now_eat.date().isoformat()
    t = now_eat.time()
    open_t      = cfg["open_t"]
    late_t      = cfg["late_t"]
    earliest_t  = cfg["earliest_open_t"]
    cutoff_t    = cfg["not_open_cutoff_t"]

    # Morning-window gate. Crossings outside 07:00 – 09:30 EAT are
    # NOT considered store-opening signals:
    #   t < 07:00  → security / cleaning, ignore silently.
    #   t > 09:30  → regular customer traffic; the metric still
    #                ticks via EntryExitDetector, but no alert fires.
    # This is the single rule that eliminates 99% of false alerts.
    if earliest_t is not None and t < earliest_t:
        return None
    if cutoff_t is not None and t > cutoff_t:
        return None

    # Idempotency — fire the "Store Opened" alert exactly ONCE per store
    # per EAT day. mark_store_opened() does a Redis SET NX on
    # vg:store:opened:{store_id}:{date_eat}, so only the FIRST inward
    # crossing across ALL of the store's entrance cameras claims the slot
    # (returns True) and fires; every later crossing sees the key and
    # returns here. Glass-door crossings record method="glass_door_crossing"
    # so downstream consumers can see which sensor cleared the marker.
    # With no store context we fall back to the per-camera dedupe.
    method = "glass_door_crossing" if via_glass_door else "entry_crossing"
    if ctx.store_id is not None:
        claimed = mark_store_opened(
            ctx.store_id, day_iso,
            opened_at=now_eat,
            method=method,
            confidence="high",
            camera_id=ctx.camera_id,
        )
        if not claimed:
            return None
    elif _already_fired(ctx.camera_id, day_iso, "open"):
        return None

    # Per-camera marker the not-opened beat also reads (belt-and-braces).
    _mark_fired(ctx.camera_id, day_iso, "open")

    glass_suffix = " — confirmed via glass door sensor" if via_glass_door else ""
    if t <= late_t:
        kind     = "shop_opened"
        priority = "info"
        message  = (f"Shop opened at {now_eat.strftime('%H:%M')}{glass_suffix}"
                    if via_glass_door else
                    f"Shop open at {now_eat.strftime('%H:%M')}")
    else:
        # Minutes late relative to the trading start so operators see
        # how far past opening time the store actually opened.
        minutes_late = (
            (t.hour * 60 + t.minute) - (open_t.hour * 60 + open_t.minute))
        kind     = "shop_opened_late"
        priority = "warning"
        message  = (f"Shop opened late at {now_eat.strftime('%H:%M')}"
                    f"{glass_suffix} "
                    f"({minutes_late} min after {open_t.strftime('%H:%M')} start)")

    log.info("shop_open_close: %s cam=%s eat=%s via_glass_door=%s",
             kind, ctx.camera_id, now_eat.isoformat(), via_glass_door)

    return DetectionEvent(
        detection_type=SHOP_OPEN_CLOSE_TYPE,
        cls=kind, confidence=1.0,
        bbox_norm=bbox_norm, track_id=track_id, zone_id=zone_id,
        extra={
            "priority":         priority,
            "rule":             kind,
            "store_id":         ctx.store_id,
            "message":          message,
            "eat_time":         now_eat.strftime("%H:%M"),
            "eat_iso":          now_eat.isoformat(),
            "trading_start":    open_t.strftime("%H:%M"),
            "late_threshold":   late_t.strftime("%H:%M"),
            "shutter_state":    "open",
            "signal":           ("glass_door_crossing" if via_glass_door
                                  else "shutter_open+inward_crossing"),
            "signal_confidence": "high" if via_glass_door else "medium",
            "via_glass_door":   via_glass_door,
        },
    )


# ---------- close-alert decision -----------------------------------

def maybe_emit_close_alert(ctx: DetectorContext, cfg_extra: dict | None,
                            track_id: int, zone_id: int | None,
                            bbox_norm: list[float],
                            *, via_glass_door: bool = False) -> DetectionEvent | None:
    """Called from EntryExitDetector on every outward crossing. Fires
    once per camera per EAT day on the FIRST outward crossing after
    the configured closing threshold.

    `via_glass_door=True` is passed by the detector when the
    originating zone is glass_door-tagged. The 2-frame persistence
    filter has already cleared reflections; we surface that as
    `signal="glass_door_crossing"` + a stronger plain-English
    message."""
    cfg = _read_cfg(cfg_extra)
    if not cfg["enabled"] or not cfg["close_t"]:
        return None

    now_eat = _now_eat()
    t = now_eat.time()
    # Closing window: only outward crossings between close_t (20:00
    # default) and latest_close_t (22:00 default) count as closing
    # signals. Outside this band an outward crossing is a customer
    # leaving early or a very late shopper, not the close-of-day.
    if t < cfg["close_t"]:
        return None
    if cfg["latest_close_t"] is not None and t > cfg["latest_close_t"]:
        return None

    day_iso = now_eat.date().isoformat()
    if _already_fired(ctx.camera_id, day_iso, "close"):
        return None

    _mark_fired(ctx.camera_id, day_iso, "close")
    log.info("shop_open_close: shop_closed cam=%s eat=%s via_glass_door=%s",
             ctx.camera_id, now_eat.isoformat(), via_glass_door)

    message = (f"Shop closed at {now_eat.strftime('%H:%M')} "
               f"— confirmed via glass door sensor") if via_glass_door \
              else f"Shop closed at {now_eat.strftime('%H:%M')}"

    return DetectionEvent(
        detection_type=SHOP_OPEN_CLOSE_TYPE,
        cls="shop_closed", confidence=1.0,
        bbox_norm=bbox_norm, track_id=track_id, zone_id=zone_id,
        extra={
            "priority":            "info",
            "rule":                "shop_closed",
            "store_id":            ctx.store_id,
            "message":             message,
            "eat_time":            now_eat.strftime("%H:%M"),
            "eat_iso":             now_eat.isoformat(),
            "closing_threshold":   cfg["close_t"].strftime("%H:%M"),
            "signal":              ("glass_door_crossing" if via_glass_door
                                     else "outward_crossing_after_close_threshold"),
            "signal_confidence":   "high" if via_glass_door else "medium",
            "via_glass_door":      via_glass_door,
        },
    )
