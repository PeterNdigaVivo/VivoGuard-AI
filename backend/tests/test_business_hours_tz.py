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
    _store_local_now, intrusion_time_context, is_open,
    is_open_with_default, is_store_open, localised_now,
    store_time_context,
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


# ---- is_open_with_default — the Aug-2026 false-URGENT follow-up -----
# is_open() is closed-by-default, so {} / a missing weekday key /
# malformed window strings armed the intrusion detector ALL DAY and
# fired staff_present_after_hours at 10:55 EAT while stores were open.

AT_1055_EAT = datetime(2026, 8, 6, 10, 55, tzinfo=EAT)   # Thursday
AT_0300_EAT = datetime(2026, 8, 6, 3, 0, tzinfo=EAT)
AT_2030_EAT = datetime(2026, 8, 6, 20, 30, tzinfo=EAT)


def test_default_configured_hours_still_honoured() -> None:
    assert is_open_with_default(HOURS, AT_1055_EAT) is True
    assert is_open_with_default(HOURS, AT_2030_EAT) is False  # 20:00 close
    assert is_open_with_default(HOURS, AT_0300_EAT) is False


def test_default_empty_dict_uses_default_window_not_closed() -> None:
    # The reported incident shape: hours never filled in.
    assert is_open_with_default({}, AT_1055_EAT) is True
    assert is_open_with_default(None, AT_1055_EAT) is True
    assert is_open_with_default({}, AT_0300_EAT) is False     # still armed overnight


def test_default_missing_weekday_key_uses_default_window() -> None:
    assert is_open_with_default({"mon": ["09:00-20:00"]}, AT_1055_EAT) is True


def test_default_malformed_windows_use_default_window() -> None:
    for bad in (["9:00 - 20:00"], ["0900-2000"], ["garbage"]):
        assert is_open_with_default({"thu": bad}, AT_1055_EAT) is True, bad
        assert is_open_with_default({"thu": bad}, AT_0300_EAT) is False, bad


def test_default_explicitly_closed_day_stays_armed() -> None:
    # "thu": [] is a deliberate closed-all-day config — intrusion
    # detection must stay armed, unlike the unconfigured cases above.
    assert is_open_with_default({"thu": []}, AT_1055_EAT) is False


# ---- intrusion_time_context — before- vs after-hours wording --------

AT_0730_EAT = datetime(2026, 8, 6, 7, 30, tzinfo=EAT)
AT_2130_EAT = datetime(2026, 8, 6, 21, 30, tzinfo=EAT)


def test_time_context_before_vs_after() -> None:
    assert intrusion_time_context(HOURS, AT_0730_EAT) == "before_hours"
    assert intrusion_time_context(HOURS, AT_0300_EAT) == "before_hours"
    assert intrusion_time_context(HOURS, AT_2130_EAT) == "after_hours"


def test_time_context_unconfigured_uses_default_window() -> None:
    # No hours → default 09:00-21:00 boundary decides the wording.
    assert intrusion_time_context({}, AT_0730_EAT) == "before_hours"
    assert intrusion_time_context(None, AT_2130_EAT) == "after_hours"


def test_time_context_explicitly_closed_day_is_after_hours() -> None:
    # Closed all day → no opening to be "before" → after_hours label.
    assert intrusion_time_context({"thu": []}, AT_0730_EAT) == "after_hours"


def test_store_time_context_wraps_orm_row() -> None:
    store = SimpleNamespace(timezone="Africa/Nairobi",
                            business_hours_json=HOURS)
    # 04:30 UTC == 07:30 EAT — before the 09:00 open.
    at_0430_utc = datetime(2026, 8, 6, 4, 30, tzinfo=timezone.utc)
    assert store_time_context(store, at_0430_utc) == "before_hours"
    # 18:30 UTC == 21:30 EAT — past the 20:00 close.
    at_1830_utc = datetime(2026, 8, 6, 18, 30, tzinfo=timezone.utc)
    assert store_time_context(store, at_1830_utc) == "after_hours"
