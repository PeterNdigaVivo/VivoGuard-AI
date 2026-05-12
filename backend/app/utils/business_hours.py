"""Evaluate whether a timestamp falls within a store's business hours.

`business_hours_json` shape:
    {"mon": ["09:00-20:00"],            # one window
     "fri": ["09:00-13:00", "14:00-21:00"],   # split shift
     "sun": []}                          # closed all day

Used by the Intrusion detector (P1) to flag motion outside business
hours, and by the Shutter detector (P2) for inverse checks.
"""
from __future__ import annotations
from datetime import datetime, time
from typing import Optional

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
