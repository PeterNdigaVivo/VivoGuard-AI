"""_time_context — the read-time before/after-hours resolver behind
alert titles/bodies (api/alerts.py).

Regression for the Aug-2026 report: person alerts at 07:49 EAT still
read "Person Detected After Hours" because the person branches never
consulted time_context. The resolver prefers the stamped
extra.time_context and falls back to recomputing from the store's
hours at the EVENT timestamp (covers person events and pre-stamp rows).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.alerts import _time_context

HOURS = {k: ["09:00-20:00"] for k in
         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
STORE = SimpleNamespace(timezone="Africa/Nairobi", business_hours_json=HOURS)

# 04:49 UTC == 07:49 EAT — the reported card's timestamp.
T_0749_EAT = datetime(2026, 8, 6, 4, 49, tzinfo=timezone.utc)
# 18:30 UTC == 21:30 EAT — past the 20:00 close.
T_2130_EAT = datetime(2026, 8, 6, 18, 30, tzinfo=timezone.utc)


def _ev(extra=None, ts=None):
    return SimpleNamespace(extra=extra or {}, timestamp=ts)


def test_stamped_extra_wins() -> None:
    assert _time_context(_ev({"time_context": "before_hours"}, T_2130_EAT),
                         STORE) == "before_hours"
    assert _time_context(_ev({"time_context": "after_hours"}, T_0749_EAT),
                         STORE) == "after_hours"


def test_unstamped_recomputes_from_event_timestamp() -> None:
    # The reported bug shape: person event, no stamp, 07:49 EAT.
    assert _time_context(_ev(ts=T_0749_EAT), STORE) == "before_hours"
    assert _time_context(_ev(ts=T_2130_EAT), STORE) == "after_hours"


def test_naive_timestamp_treated_as_utc() -> None:
    naive = T_0749_EAT.replace(tzinfo=None)
    assert _time_context(_ev(ts=naive), STORE) == "before_hours"


def test_no_store_no_stamp_defaults_after_hours() -> None:
    assert _time_context(_ev(ts=T_0749_EAT), None) == "after_hours"
    assert _time_context(_ev(), STORE) == "after_hours"
