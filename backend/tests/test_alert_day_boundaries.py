from datetime import datetime, timezone

from app.api.alerts import _eat_day_bounds


def test_alert_summary_day_is_anchored_to_nairobi_midnight():
    # 00:30 on 21 August in Nairobi is still 20 August in UTC. The summary
    # must nevertheless start at the operator's local midnight (21:00 UTC).
    now = datetime(2026, 8, 20, 21, 30, tzinfo=timezone.utc)

    yesterday, today, tomorrow = _eat_day_bounds(now)

    assert yesterday == datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc)
    assert today == datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)
    assert tomorrow == datetime(2026, 8, 21, 21, 0, tzinfo=timezone.utc)


def test_alert_summary_day_remains_24_hours_across_month_boundary():
    now = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

    yesterday, today, tomorrow = _eat_day_bounds(now)

    assert today - yesterday == tomorrow - today
    assert (tomorrow - today).total_seconds() == 24 * 60 * 60
    assert today == datetime(2026, 8, 31, 21, 0, tzinfo=timezone.utc)
