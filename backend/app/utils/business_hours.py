"""Evaluate whether a timestamp falls within a store's business hours.

`business_hours_json` shape:
    {"mon": ["09:00-20:00"],            # one window
     "fri": ["09:00-13:00", "14:00-21:00"],   # split shift
     "sun": []}                          # closed all day

Used by the Intrusion detector (P1) to flag motion outside business
hours, the Shutter detector (P2) for inverse checks, and the May-2026
dashboard redesign to gate KPI tiles behind "store currently open".
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Timezones we've already warned about — one log line per bad value,
# not one per frame (these helpers run in the per-frame detector path).
_WARNED_BAD_TZ: set[str] = set()

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# When a store has no business_hours_json at all, fall back to the
# dominant Vivo schedule (09:00-21:00 every day). `is_open()` stays
# closed-by-default for callers that want strict semantics; the
# intrusion gate uses `is_open_with_default()` so unconfigured hours
# arm the detector overnight (outside the default window) instead of
# around the clock.
_DASHBOARD_DEFAULT_WINDOWS = ["09:00-21:00"]
ACTUAL_CLOSE_KEY_FMT = "vg:store:last_closed:{store_id}"


def normalise_business_hours(business_hours: Optional[dict]) -> Optional[dict]:
    """Return the canonical ``{"mon": ["09:00-20:00"]}`` shape.

    Early VivoOps records used full weekday names and an object value,
    for example ``{"monday": {"open": "09:30", "close": "20:00"}}``.
    Treating that object as a list yields the strings ``open`` and
    ``close`` and silently activates the fleet default instead of the
    configured hours.  Read both shapes so existing stores remain safe;
    all new writes continue to use the canonical format.
    """
    if not isinstance(business_hours, dict):
        return business_hours

    canonical: dict[str, list[str]] = {}
    for short, full in zip(WEEKDAY_KEYS, WEEKDAY_NAMES):
        if short in business_hours:
            raw = business_hours[short]
        elif full in business_hours:
            raw = business_hours[full]
        else:
            continue

        if isinstance(raw, dict):
            start = raw.get("open") or raw.get("start")
            end = raw.get("close") or raw.get("end")
            canonical[short] = (
                [f"{start}-{end}"]
                if isinstance(start, str) and isinstance(end, str)
                else []
            )
        elif isinstance(raw, str):
            canonical[short] = [raw]
        elif isinstance(raw, (list, tuple)):
            canonical[short] = [value for value in raw if isinstance(value, str)]
        else:
            canonical[short] = []
    return canonical


def actual_close_grace_active(
    closed_at_epoch: object,
    *,
    now_epoch: float,
    grace_minutes: int = 30,
) -> bool:
    """Whether ``now`` is inside the grace period after observed closure.

    The marker is deliberately a plain timestamp so every alert producer can
    apply the same policy without querying Postgres on the inference hot path.
    Invalid, future or stale markers fail closed (no suppression).
    """
    try:
        elapsed = float(now_epoch) - float(closed_at_epoch)
        grace_seconds = max(0, int(grace_minutes)) * 60
    except (TypeError, ValueError, OverflowError):
        return False
    return grace_seconds > 0 and 0 <= elapsed < grace_seconds


def is_open(business_hours: Optional[dict], ts_local: datetime) -> bool:
    """True if `ts_local` falls inside any open window for that weekday.

    If `business_hours` is None or missing the day, defaults to **closed**
    (most stores opt in explicitly; closed-by-default is safer for the
    intrusion detector that fires when *closed*)."""
    business_hours = normalise_business_hours(business_hours)
    if not business_hours:
        return False
    key = WEEKDAY_KEYS[ts_local.weekday()]
    windows = business_hours.get(key) or []
    t = ts_local.time()
    for win in windows:
        try:
            a, b = win.split("-")
            ha, ma = a.split(":"); hb, mb = b.split(":")
            start = time(int(ha), int(ma))
            end   = time(int(hb), int(mb))
            if start <= t < end:
                return True
        except Exception:
            continue
    return False


def is_open_with_default(business_hours: Optional[dict],
                         ts_local: datetime) -> bool:
    """`is_open()` for the intrusion gate — unconfigured hours mean
    "assume the default Vivo schedule", not "closed".

    FALSE-URGENT BUG FIX (Aug 2026, follow-up to the tz fix): is_open()
    is closed-by-default, so business_hours_json of {} / missing
    today's key / windows that don't parse ("9:00 - 20:00") armed the
    intrusion detector ALL DAY and fired after_hours alerts at 10:55
    EAT while stores were open. Semantics here:

      * key present with parseable windows → honour them exactly
      * key present but EMPTY list        → deliberately closed all
        day (e.g. "sun": []) → stays closed, detector stays armed
      * no dict / missing key / zero parseable windows → unconfigured
        → default 09:00-21:00 window (matches _windows_for_day)
    """
    t = ts_local.time()
    return any(a <= t < b
               for a, b in _effective_windows(business_hours, ts_local.weekday()))


def _effective_windows(business_hours: Optional[dict],
                       weekday_idx: int) -> list[tuple[time, time]]:
    """Parsed windows for the weekday under is_open_with_default()
    semantics: configured+parseable → as configured; explicitly empty
    day → [] (closed all day); unconfigured / all-malformed → the
    default Vivo window."""
    business_hours = normalise_business_hours(business_hours)
    key = WEEKDAY_KEYS[weekday_idx]
    if isinstance(business_hours, dict) and key in business_hours:
        windows = list(business_hours.get(key) or [])
        if not windows:
            return []                    # explicitly closed today
        parsed = [p for p in (_parse_window(w) for w in windows) if p]
        if parsed:
            return parsed
    return [p for p in (_parse_window(w) for w in _DASHBOARD_DEFAULT_WINDOWS) if p]


def intrusion_time_context(business_hours: Optional[dict],
                           ts_local: datetime) -> str:
    """'before_hours' when ts_local is earlier than today's first
    (effective) opening time, else 'after_hours'.

    Drives the intrusion alert wording — a 07:30 person is "Someone in
    Store BEFORE Hours", a 21:30 person "... AFTER Hours". A day with
    no opening at all (explicitly closed, e.g. "sun": []) reports
    'after_hours': there is no opening to be before. Only meaningful
    while the store is closed — callers gate on that first."""
    wins = _effective_windows(business_hours, ts_local.weekday())
    if not wins:
        return "after_hours"
    first_open = min(a for a, _ in wins)
    return "before_hours" if ts_local.time() < first_open else "after_hours"


def store_time_context(store, now_utc: Optional[datetime] = None) -> str:
    """intrusion_time_context() for a Store ORM row — used by the
    after_hours_intrusion_check beat task."""
    local = _store_local_now(store, now_utc)
    return intrusion_time_context(
        getattr(store, "business_hours_json", None), local)


def localised_now(tz_name: str) -> datetime:
    """Return current time in the store's timezone.

    TIMEZONE BUG FIX (Aug 2026): the old fallback silently returned
    datetime.utcnow() whenever tz_name wasn't a valid IANA name ("EAT",
    "Nairobi", "+03:00", stray whitespace...). is_open() then compared
    07:00 UTC against 09:00-20:00 store windows and declared open
    stores CLOSED — firing after_hours intrusion alerts at 10:00 EAT.
    Every Vivo store is UTC+3 (Kenya/Uganda/Rwanda), so the safe
    fallback is Africa/Nairobi, warned once per bad value; naive UTC is
    the absolute last resort."""
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        if tz_name not in _WARNED_BAD_TZ:
            _WARNED_BAD_TZ.add(str(tz_name))
            log.warning("business_hours: invalid store timezone %r — "
                        "falling back to Africa/Nairobi", tz_name)
        try:
            return datetime.now(ZoneInfo("Africa/Nairobi"))
        except Exception:                            # pragma: no cover
            return datetime.now(timezone.utc)


# ---------- Dashboard helpers (May-2026 redesign) -------------------
#
# These wrap a Store ORM instance so callers don't have to remember to
# convert UTC into the store's timezone. They use a PERMISSIVE default
# (09:00-21:00) when business_hours_json is missing, because the
# dashboard would otherwise show "Closed" for every store the operator
# hasn't configured yet — wrong message for that audience.


def _store_local_now(store, now_utc: Optional[datetime] = None) -> datetime:
    """Convert wall-clock UTC into the store's local-timezone time
    for weekday/time-of-day comparisons."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    tz_name = getattr(store, "timezone", None) or "Africa/Nairobi"
    from zoneinfo import ZoneInfo
    try:
        return now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        # Same silent-UTC bug as localised_now — an invalid stored tz
        # must NOT flip time math to UTC on an all-EAT fleet.
        if tz_name not in _WARNED_BAD_TZ:
            _WARNED_BAD_TZ.add(str(tz_name))
            log.warning("business_hours: invalid store timezone %r — "
                        "falling back to Africa/Nairobi", tz_name)
        try:
            return now_utc.astimezone(ZoneInfo("Africa/Nairobi"))
        except Exception:                            # pragma: no cover
            return now_utc.astimezone(timezone.utc)


def _windows_for_day(store, weekday_idx: int) -> list[str]:
    bh = normalise_business_hours(getattr(store, "business_hours_json", None))
    key = WEEKDAY_KEYS[weekday_idx]
    if isinstance(bh, dict):
        if key in bh:
            return list(bh.get(key) or [])
    # Permissive default — see module comment for rationale.
    return list(_DASHBOARD_DEFAULT_WINDOWS)


def _parse_window(s: str) -> tuple[time, time] | None:
    try:
        a, b = s.split("-")
        ha, ma = (int(x) for x in a.split(":"))
        hb, mb = (int(x) for x in b.split(":"))
        return time(ha, ma), time(hb, mb)
    except Exception:
        return None


def is_store_open(store, now_utc: Optional[datetime] = None) -> bool:
    """Is `store` open right now? Wraps `is_open()` with the store's
    timezone + the permissive dashboard default."""
    local = _store_local_now(store, now_utc)
    t = local.time()
    for w in _windows_for_day(store, local.weekday()):
        parsed = _parse_window(w)
        if not parsed:
            continue
        a, b = parsed
        if a <= t < b:
            return True
    return False


def is_after_hours_with_grace(
    store,
    now_utc: Optional[datetime] = None,
    *,
    grace_before_open_min: int = 60,
    grace_after_close_min: int = 60,
) -> bool:
    """True when `now` falls OUTSIDE today's business hours AND
    OUTSIDE the configurable grace windows around the open + close
    times.

    Use this for "Person Detected After Hours" alerts so legitimate
    staff arriving shortly before the 09:00 opening (or leaving shortly
    after the 21:00 close) don't trip URGENT intrusion. 02:00 EAT
    still fires because it's well outside both grace windows.

    Grace defaults:
      grace_before_open_min = 60   (08:00–09:00 EAT for a 09:00 open)
      grace_after_close_min = 60   (21:00–22:00 EAT for a 21:00 close)

    Time math runs in the store's local timezone (Africa/Nairobi
    default) via the same path is_store_open uses.
    """
    if is_store_open(store, now_utc):
        return False
    local = _store_local_now(store, now_utc)
    t = local.time()
    grace_before = max(0, int(grace_before_open_min))
    grace_after  = max(0, int(grace_after_close_min))
    for w in _windows_for_day(store, local.weekday()):
        parsed = _parse_window(w)
        if not parsed:
            continue
        open_t, close_t = parsed
        # Minutes-of-day arithmetic — handles the hour boundary cleanly
        # without dealing with date rollover (grace ≤ 12 h always
        # stays inside the same day for retail hours).
        t_min     = t.hour * 60 + t.minute
        open_min  = open_t.hour * 60 + open_t.minute
        close_min = close_t.hour * 60 + close_t.minute
        if open_min - grace_before <= t_min < open_min:
            return False                # inside pre-opening grace
        if close_min <= t_min < close_min + grace_after:
            return False                # inside post-closing grace
    return True


def todays_session(store, now_utc: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """(open_utc, close_utc) covering today's FULL operating window
    — earliest open to latest close. Multi-window days collapse to
    their outer bounds. If the store is closed all day, returns a
    degenerate (now, now) pair so SQL queries return empty cleanly.

    Used by 'Today so far' aggregates so we never include yesterday's
    after-hours noise."""
    local = _store_local_now(store, now_utc)
    windows = sorted(
        filter(None, (_parse_window(w) for w in _windows_for_day(store, local.weekday())))
    )
    if not windows:
        anchor = datetime.now(timezone.utc) if now_utc is None else now_utc
        return anchor, anchor
    a = windows[0][0]
    b = windows[-1][1]
    day = local.date()
    open_local  = datetime.combine(day, a, tzinfo=local.tzinfo)
    close_local = datetime.combine(day, b, tzinfo=local.tzinfo)
    return open_local.astimezone(timezone.utc), close_local.astimezone(timezone.utc)


def todays_hours_label(store, now_utc: Optional[datetime] = None) -> str:
    """Human-readable hours string for the dashboard top bar.

    Examples: '09:00–21:00', 'Closed today', '09:00–13:00, 14:00–20:00'.
    """
    local = _store_local_now(store, now_utc)
    parts: list[str] = []
    for w in _windows_for_day(store, local.weekday()):
        parsed = _parse_window(w)
        if not parsed:
            continue
        a, b = parsed
        parts.append(f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}")
    return ", ".join(parts) if parts else "Closed today"
