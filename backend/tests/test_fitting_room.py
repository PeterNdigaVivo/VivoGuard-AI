from datetime import datetime, timedelta, timezone

from app.tasks.fitting_room import replay_occupancy

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
MAX_STAY = 1800


def at(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def test_entry_then_exit_leaves_nobody_inside():
    assert replay_occupancy([(at(5), "in"), (at(2), "out")], NOW, MAX_STAY) == (0, None)


def test_open_entries_are_counted_and_oldest_reported():
    assert replay_occupancy([(at(8), "in"), (at(3), "in")], NOW, MAX_STAY) == (2, at(8))


def test_exit_closes_the_oldest_entry_so_long_stays_are_never_invented():
    occupancy, oldest = replay_occupancy(
        [(at(12), "in"), (at(4), "in"), (at(1), "out")], NOW, MAX_STAY)
    assert occupancy == 1
    assert oldest == at(4)


def test_entry_whose_exit_was_missed_ages_out_of_the_window():
    # Entered 40 min ago with no recorded exit: outside the 30-min window.
    assert replay_occupancy([(at(40), "in")], NOW, MAX_STAY) == (0, None)


def test_exit_without_a_recorded_entry_never_goes_negative():
    assert replay_occupancy([(at(2), "out")], NOW, MAX_STAY) == (0, None)


def test_crossings_are_replayed_in_time_order_whatever_the_input_order():
    occupancy, oldest = replay_occupancy(
        [(at(1), "out"), (at(9), "in"), (at(6), "in")], NOW, MAX_STAY)
    assert occupancy == 1
    assert oldest == at(6)


def test_unrecognised_directions_are_ignored():
    assert replay_occupancy([(at(3), "sideways"), (at(2), "")], NOW, MAX_STAY) == (0, None)
