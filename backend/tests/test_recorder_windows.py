"""Recorder coverage must include the after-hours intrusion period."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.tasks.recorder import _current_window


EAT = ZoneInfo("Africa/Nairobi")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 21, hour, minute, tzinfo=EAT)


def test_recorder_has_no_daily_coverage_gap() -> None:
    assert all(_current_window(_at(hour, 30)) is not None for hour in range(24))


def test_after_hours_windows_are_bounded_and_date_stamped() -> None:
    midnight = _current_window(_at(3, 22))
    evening = _current_window(_at(23, 59))
    assert midnight is not None and midnight[:2] == ("20260821_0000", 25200)
    assert evening is not None and evening[:2] == ("20260821_2000", 14400)
