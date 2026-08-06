"""Timezone regression tests for app.utils.business_hours.

The Aug-2026 bug: an invalid store timezone string made localised_now /
_store_local_now silently fall back to UTC, so is_open() compared 07:00
UTC against 09:00-20:00 EAT windows and fired after_hours intrusion
alerts while stores were open. The fallback is now Africa/Nairobi.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.utils.business_hours import (
    _store_local_now, is_open, is_store_open, localised_now,
)

EAT = ZoneInfo("Africa/Nairobi")
HOURS = {k: ["09:00-20:00"] for k in
         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
# 07:00 UTC == 10:00 EAT — the exact reported failure moment.
T0700_UTC = datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)


def test_localised_now_valid_tz() -> None:
    assert localised_now("Africa/Nairobi").utcoffset() == \
        datetime.now(EAT).utcoffset()


def test_localised_now_invalid_tz_falls_back_to_eat_not_utc() -> None:
    for bad in ("EAT", "Nairobi", "+03:00", "", "Africa/Nairobi "):
        got = localised_now(bad)
        assert got.utcoffset() == datetime.now(EAT).utcoffset(), bad


def test_store_local_now_invalid_tz_falls_back_to_eat() -> None:
    store = SimpleNamespace(timezone="EAT", business_hours_json=HOURS)
    local = _store_local_now(store, T0700_UTC)
    assert local.hour == 10                       # 07:00 UTC -> 10:00 EAT


def test_open_store_reads_open_despite_garbage_tz() -> None:
    # The reported bug: 10:00 EAT read as 07:00 UTC -> "closed" ->
    # after_hours intrusion alerts during trading hours.
    store = SimpleNamespace(timezone="EAT", business_hours_json=HOURS)
    assert is_store_open(store, T0700_UTC) is True


def test_is_open_pure_window_math_unchanged() -> None:
    at_10_eat = T0700_UTC.astimezone(EAT)
    assert is_open(HOURS, at_10_eat) is True
    at_23_eat = datetime(2026, 8, 6, 20, 0, tzinfo=EAT)
    assert is_open(HOURS, at_23_eat) is False     # closes at 20:00
