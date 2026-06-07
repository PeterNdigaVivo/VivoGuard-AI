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
DEFAULT_LATE_THRESHOLD  = "09:33"
DEFAULT_CLOSING_THRESH  = "20:15"

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
    }


# ---------- open-alert decision ------------------------------------

def maybe_emit_open_alert(ctx: DetectorContext, cfg_extra: dict | None,
                           track_id: int, zone_id: int | None,
                           bbox_norm: list[float]) -> DetectionEvent | None:
    """Called from EntryExitDetector on every inward crossing. Returns
    a DetectionEvent to append to its output, or None.

    Requires BOTH:
      - ShutterDetector has committed state 'open' on this camera.
      - This is the first qualifying crossing today (per-camera-per-
        day dedupe across all three open bands).
    """
    cfg = _read_cfg(cfg_extra)
    if not cfg["enabled"]:
        return None
    if not (cfg["open_t"] and cfg["late_t"]):
        return None

    # Signal-agreement gate.
    if get_shutter_state(ctx.camera_id) != "open":
        return None

    now_eat = _now_eat()
    day_iso = now_eat.date().isoformat()

    # One shared dedupe key across the three open bands — the spec
    # says "fire ONCE per camera per day" for the open alert family,
    # regardless of which band ended up triggering.
    if _already_fired(ctx.camera_id, day_iso, "open"):
        return None

    t = now_eat.time()
    open_t  = cfg["open_t"]
    late_t  = cfg["late_t"]

    if t < open_t:
        kind     = "shop_opened_before_hours"
        priority = "high"
        message  = f"Shop opened before trading hours at {now_eat.strftime('%H:%M')}"
    elif t <= late_t:
        kind     = "shop_opened"
        priority = "info"
        message  = f"Shop open at {now_eat.strftime('%H:%M')}"
    else:
        # Minutes late relative to the trading start, not the late
        # threshold — operators care about how far past opening time.
        minutes_late = (
            (t.hour * 60 + t.minute) - (open_t.hour * 60 + open_t.minute))
        kind     = "shop_opened_late"
        priority = "warning"
        message  = (f"Shop opened late at {now_eat.strftime('%H:%M')} "
                    f"({minutes_late} min after {open_t.strftime('%H:%M')} start)")

    _mark_fired(ctx.camera_id, day_iso, "open")
    log.info("shop_open_close: %s cam=%s eat=%s",
             kind, ctx.camera_id, now_eat.isoformat())

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
            "signal":           "shutter_open+inward_crossing",
        },
    )


# ---------- close-alert decision -----------------------------------

def maybe_emit_close_alert(ctx: DetectorContext, cfg_extra: dict | None,
                            track_id: int, zone_id: int | None,
                            bbox_norm: list[float]) -> DetectionEvent | None:
    """Called from EntryExitDetector on every outward crossing. Fires
    once per camera per EAT day on the FIRST outward crossing after
    the configured closing threshold."""
    cfg = _read_cfg(cfg_extra)
    if not cfg["enabled"] or not cfg["close_t"]:
        return None

    now_eat = _now_eat()
    if now_eat.time() < cfg["close_t"]:
        return None

    day_iso = now_eat.date().isoformat()
    if _already_fired(ctx.camera_id, day_iso, "close"):
        return None

    _mark_fired(ctx.camera_id, day_iso, "close")
    log.info("shop_open_close: shop_closed cam=%s eat=%s",
             ctx.camera_id, now_eat.isoformat())

    return DetectionEvent(
        detection_type=SHOP_OPEN_CLOSE_TYPE,
        cls="shop_closed", confidence=1.0,
        bbox_norm=bbox_norm, track_id=track_id, zone_id=zone_id,
        extra={
            "priority":            "info",
            "rule":                "shop_closed",
            "store_id":            ctx.store_id,
            "message":             f"Shop closed at {now_eat.strftime('%H:%M')}",
            "eat_time":            now_eat.strftime("%H:%M"),
            "eat_iso":             now_eat.isoformat(),
            "closing_threshold":   cfg["close_t"].strftime("%H:%M"),
            "signal":              "outward_crossing_after_close_threshold",
        },
    )
