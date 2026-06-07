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
from datetime import datetime, time, timezone
from typing import Optional

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# When a store has no business_hours_json at all, fall back to the
# dominant Vivo schedule (09:00-21:00 every day) for the DASHBOARD
# helpers below. The DETECTOR helper `is_open()` stays closed-by-
# default — we don't want to silence intrusion alarms because nobody
# filled the field in.
_DASHBOARD_DEFAULT_WINDOWS = ["09:00-21:00"]


def is_open(business_hours: Optional[dict], ts_local: datetime) -> bool:
    """True if `ts_local` falls inside any open window for that weekday.

    If `business_hours` is None or missing the day, defaults to **closed**
    (most stores opt in explicitly; closed-by-default is safer for the
    intrusion detector that fires when *closed*)."""
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


def localised_now(tz_name: str) -> datetime:
    """Return current time in the store's timezone (or UTC fallback)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.utcnow()


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
    try:
        from zoneinfo import ZoneInfo
        return now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return now_utc.astimezone(timezone.utc)


def _windows_for_day(store, weekday_idx: int) -> list[str]:
    bh = getattr(store, "business_hours_json", None)
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
