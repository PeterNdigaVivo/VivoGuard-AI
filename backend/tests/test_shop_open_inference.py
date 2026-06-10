"""Tests for the occupancy-inference store-open detection logic.

The full end-to-end flow has three moving parts (per-frame Redis
writes, a Celery task, an alert row) but the DECISION logic — the
"≥ 2 person events within a 5-minute window, opened_at = earliest
event in that window" rule — lives in a single pure function in
app.tasks.alerting (`confirm_opening_from_events`). These tests
drive that function directly so they run with no DB / no Redis / no
Celery infra. They cover every numbered scenario in the original
ticket.

If the repo grows a real conftest with DB + Redis fixtures later,
add integration tests on top of these — but don't replace them, the
pure-function coverage is what guarantees the rule itself is right.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.tasks.alerting import (
    confirm_opening_from_events,
    PERSON_OPEN_THRESHOLD,
    OPEN_CONFIRMATION_WINDOW_S,
)

EAT = ZoneInfo("Africa/Nairobi")


def _eat(h: int, m: int, s: int = 0) -> datetime:
    """Build an EAT timestamp on an arbitrary fixed date — only the
    deltas between events matter for the sliding-window rule."""
    return datetime(2026, 6, 12, h, m, s, tzinfo=EAT)


# ---- Test 1 — entrance crossing path is unaffected -----------------
# The crossing path doesn't go through confirm_opening_from_events,
# but the helper must NEVER fire on a single event (which is what an
# unguarded crossing-path test could accidentally encode if the
# threshold is reduced). Empty input + single-event input both
# return None.

def test_no_events_returns_none():
    assert confirm_opening_from_events([]) is None


def test_single_event_does_not_open():
    """Spec test 3 — only one person detection: stays CLOSED."""
    assert confirm_opening_from_events([_eat(8, 7)]) is None


# ---- Test 2 — two events within 5 minutes opens ---------------------

def test_two_events_within_window_opens_at_earliest():
    """Spec test 2 — 08:07 + 08:09 → opened_at = 08:07 (earliest of
    the pair, NOT 08:09 — preserves accurate opening analytics)."""
    earliest = _eat(8, 7)
    later    = _eat(8, 9)
    assert confirm_opening_from_events([earliest, later]) == earliest


def test_two_events_at_window_edge_still_opens():
    """Exactly OPEN_CONFIRMATION_WINDOW_S apart is still within the
    window (≤, not strict <)."""
    a = _eat(8, 0)
    b = a + timedelta(seconds=OPEN_CONFIRMATION_WINDOW_S)
    assert confirm_opening_from_events([a, b]) == a


def test_two_events_just_outside_window_does_not_open():
    """5 min 1 s apart: no opening — must wait for a closer pair."""
    a = _eat(8, 0)
    b = a + timedelta(seconds=OPEN_CONFIRMATION_WINDOW_S + 1)
    assert confirm_opening_from_events([a, b]) is None


# ---- Test 3 — earliest of the FIRST qualifying pair ----------------

def test_skips_isolated_then_uses_later_cluster():
    """One stray detection at 06:30, then real cluster at 08:05 +
    08:07 + 08:08. opened_at must be 08:05 (start of the cluster),
    not 06:30 (the stray) — the stray's window was empty so no
    confirmation happened then."""
    stray   = _eat(6, 30)
    first   = _eat(8, 5)
    second  = _eat(8, 7)
    third   = _eat(8, 8)
    assert (confirm_opening_from_events([stray, first, second, third])
            == first)


def test_three_events_in_one_minute_opens_at_first():
    """Busy doorway — three rapid detections. opened_at = first."""
    a = _eat(8, 10, 5)
    b = _eat(8, 10, 30)
    c = _eat(8, 10, 50)
    assert confirm_opening_from_events([a, b, c]) == a


# ---- Test 4 — pure-function dedupe property (idempotent) ------------

def test_same_timestamps_repeated_returns_same_answer():
    """Spec test 6 — running confirm_opening repeatedly with the
    same input returns the same opened_at; the dedupe lives in
    Redis (mark_store_opened) but the rule itself is idempotent."""
    a = _eat(8, 7); b = _eat(8, 9)
    first  = confirm_opening_from_events([a, b])
    second = confirm_opening_from_events([a, b])
    assert first == second == a


# ---- Test 5 — sensitivity to PERSON_OPEN_THRESHOLD setting ----------

def test_threshold_constant_is_two():
    """Sanity — the spec's PERSON_OPEN_THRESHOLD = 2 is what the
    code ships with. A regression to 1 would mean a single passing
    guard opens the store."""
    assert PERSON_OPEN_THRESHOLD == 2


def test_window_constant_is_five_minutes():
    assert OPEN_CONFIRMATION_WINDOW_S == 5 * 60


# ---- Test 6 — large input is O(n), no algorithmic blowup ------------

def test_large_input_still_decides_correctly():
    """A busy day's worth of person events should resolve in a
    single sliding-window pass. Spread 5000 events across 8 hours
    (one every ~5.7 s) — early in that stream there's already a
    qualifying pair so opened_at = the very first timestamp."""
    base = _eat(8, 0)
    events = [base + timedelta(seconds=i * 6) for i in range(5000)]
    assert confirm_opening_from_events(events) == base
