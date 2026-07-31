"""Unit tests for the PURE evaluate_activity_rules function.

Everything is driven with plain dicts — no Redis, no DB. (Dedupe-window
behaviour is a beat-task concern, exercised in the integration tests.)
"""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")          # module imports celery→settings
pytest.importorskip("celery")
pytest.importorskip("redis")

from app.tasks.activity_sentinel import evaluate_activity_rules  # noqa: E402

NOW = 1_800_000_000.0

CFG = {"surge_people": 12, "surge_sustain_samples": 3,
       "store_surge_people": 30, "dead_scene_minutes": 0}


def _win(*people: int, score: float | None = None,
         start_ts: float = NOW - 540) -> list[dict]:
    """Window oldest→newest, one sample per minute ending at NOW."""
    return [{"people": p,
             "score": float(p) if score is None else score,
             "ts": start_ts + i * 60}
            for i, p in enumerate(people)]


def _eval(samples, store_map, cfg=CFG, *, store_open=None, fresh=None,
          intrusion=None, overrides=None, now=NOW):
    return evaluate_activity_rules(
        samples, store_map, cfg,
        store_open=store_open or {},
        fresh_frame_cams=fresh or set(),
        intrusion_active_stores=intrusion or set(),
        overrides=overrides, now_ts=now)


# ── rule a: occupancy_surge ────────────────────────────────────────────────

def test_surge_fires_after_sustain() -> None:
    out = _eval({1: _win(5, 13, 14, 15)}, {1: 10}, store_open={10: True})
    rules = [t["rule"] for t in out]
    assert "occupancy_surge" in rules
    t = next(t for t in out if t["rule"] == "occupancy_surge")
    assert t["camera_id"] == 1 and t["store_id"] == 10
    assert t["severity"] == "ATTENTION"
    assert t["extra"] == {"people_count": 15, "threshold": 12,
                          "sustain_samples": 3}


def test_surge_requires_consecutive_samples() -> None:
    # Dip in the middle of the last 3 → no fire.
    out = _eval({1: _win(13, 14, 5, 15)}, {1: 10}, store_open={10: True})
    assert all(t["rule"] != "occupancy_surge" for t in out)


def test_surge_short_window_never_fires() -> None:
    out = _eval({1: _win(20, 20)}, {1: 10}, store_open={10: True})
    assert all(t["rule"] != "occupancy_surge" for t in out)


# ── rule: activity_presence ───────────────────────────────────────────────

CFG_P = {**CFG, "presence_enabled": True, "presence_threshold": 1,
         "presence_sustain_samples": 2}


def test_activity_presence_fires_on_low_count() -> None:
    # 1 person for 2 consecutive samples → fires at INFO.
    out = _eval({1: _win(1, 1)}, {1: 10}, CFG_P, store_open={10: True})
    t = next(t for t in out if t["rule"] == "activity_presence")
    assert t["severity"] == "INFO"
    assert t["camera_id"] == 1
    assert t["extra"] == {"people_count": 1, "threshold": 1,
                          "sustain_samples": 2}
    # 1 person for only 1 sample → does NOT fire.
    out = _eval({1: _win(1)}, {1: 10}, CFG_P, store_open={10: True})
    assert all(t["rule"] != "activity_presence" for t in out)


def test_activity_presence_skips_closed_stores() -> None:
    # After-hours presence belongs to after_hours_activity (URGENT),
    # not the INFO presence rule — no double-fire.
    out = _eval({1: _win(1, 1)}, {1: 10}, CFG_P, store_open={10: False})
    assert all(t["rule"] != "activity_presence" for t in out)
    assert any(t["rule"] == "after_hours_activity" for t in out)


def test_activity_presence_off_when_unconfigured() -> None:
    # Pure-function contract: a config without presence_enabled means
    # the rule is off (production passes the setting explicitly).
    out = _eval({1: _win(1, 1)}, {1: 10}, store_open={10: True})
    assert all(t["rule"] != "activity_presence" for t in out)


def test_activity_presence_zero_people_never_fires() -> None:
    out = _eval({1: _win(0, 0)}, {1: 10}, CFG_P, store_open={10: True})
    assert all(t["rule"] != "activity_presence" for t in out)


# ── rule b: store_surge ────────────────────────────────────────────────────

def test_store_surge_aggregates_and_anchors_busiest() -> None:
    samples = {1: _win(16, 16, 16), 2: _win(18, 18, 18)}
    out = _eval(samples, {1: 10, 2: 10}, store_open={10: True})
    t = next(t for t in out if t["rule"] == "store_surge")
    assert t["store_id"] == 10
    assert t["camera_id"] == 2                      # busiest latest sample
    assert t["extra"]["people_count"] == 34
    assert t["extra"]["camera_ids"] == [1, 2]


def test_store_surge_slot_alignment_blocks_offset_spikes() -> None:
    # Each camera spiked on DIFFERENT ticks; no single tick sums >= 30.
    samples = {1: _win(28, 2, 2), 2: _win(2, 2, 26)}
    out = _eval(samples, {1: 10, 2: 10}, store_open={10: True})
    assert all(t["rule"] != "store_surge" for t in out)


def test_store_surge_single_trigger_per_store() -> None:
    samples = {1: _win(20, 20, 20), 2: _win(20, 20, 20)}
    out = _eval(samples, {1: 10, 2: 10}, store_open={10: True})
    assert sum(1 for t in out if t["rule"] == "store_surge") == 1


def test_unattached_camera_excluded_from_store_rules() -> None:
    out = _eval({1: _win(40, 40, 40)}, {1: None}, store_open={})
    assert all(t["rule"] != "store_surge" for t in out)
    assert any(t["rule"] == "occupancy_surge" for t in out)   # cam rule OK


# ── rule c: after_hours_activity ───────────────────────────────────────────

def test_after_hours_fires_when_store_closed() -> None:
    out = _eval({1: _win(0, 0, 2)}, {1: 10}, store_open={10: False})
    t = next(t for t in out if t["rule"] == "after_hours_activity")
    assert t["severity"] == "URGENT"
    assert t["store_id"] == 10 and t["camera_id"] == 1
    assert t["extra"]["people_count"] == 2


def test_after_hours_suppressed_by_intrusion_session() -> None:
    out = _eval({1: _win(0, 0, 2)}, {1: 10}, store_open={10: False},
                intrusion={10})
    assert all(t["rule"] != "after_hours_activity" for t in out)


def test_after_hours_skips_open_or_unknown_stores() -> None:
    out = _eval({1: _win(3), 2: _win(3)}, {1: 10, 2: 11},
                store_open={10: True})           # 11 unknown
    assert all(t["rule"] != "after_hours_activity" for t in out)


def test_after_hours_one_trigger_per_store_busiest_anchor() -> None:
    out = _eval({1: _win(1), 2: _win(4)}, {1: 10, 2: 10},
                store_open={10: False})
    hits = [t for t in out if t["rule"] == "after_hours_activity"]
    assert len(hits) == 1
    assert hits[0]["camera_id"] == 2
    assert hits[0]["extra"]["camera_ids"] == [1, 2]


# ── rule d: dead_scene ─────────────────────────────────────────────────────

def test_dead_scene_disabled_at_zero_minutes() -> None:
    samples = {1: _win(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, score=0.0)}
    out = _eval(samples, {1: 10}, store_open={10: True}, fresh={1})
    assert all(t["rule"] != "dead_scene" for t in out)


def test_dead_scene_fires_when_enabled_and_flat_zero() -> None:
    cfg = {**CFG, "dead_scene_minutes": 5}
    samples = {1: _win(0, 0, 0, 0, 0, 0, 0, score=0.0)}   # 6-min span
    out = _eval(samples, {1: 10}, cfg, store_open={10: True}, fresh={1})
    t = next(t for t in out if t["rule"] == "dead_scene")
    assert t["severity"] == "WARNING"
    assert t["extra"]["threshold_minutes"] == 5


def test_dead_scene_requires_fresh_frame_and_open_store() -> None:
    cfg = {**CFG, "dead_scene_minutes": 5}
    samples = {1: _win(0, 0, 0, 0, 0, 0, 0, score=0.0)}
    # No fresh frame → streamer agent's problem, not ours.
    out = _eval(samples, {1: 10}, cfg, store_open={10: True}, fresh=set())
    assert all(t["rule"] != "dead_scene" for t in out)
    # Store closed → dead scenes overnight are normal.
    out = _eval(samples, {1: 10}, cfg, store_open={10: False}, fresh={1})
    assert all(t["rule"] != "dead_scene" for t in out)


def test_dead_scene_any_detection_resets() -> None:
    cfg = {**CFG, "dead_scene_minutes": 5}
    w = _win(0, 0, 0, 0, 0, 0, 0, score=0.0)
    w[3]["score"] = 1.0                            # one detection mid-window
    out = _eval({1: w}, {1: 10}, cfg, store_open={10: True}, fresh={1})
    assert all(t["rule"] != "dead_scene" for t in out)


# ── overrides ──────────────────────────────────────────────────────────────

def test_override_disabled_camera_skipped_everywhere() -> None:
    samples = {1: _win(20, 20, 20), 2: _win(20, 20, 20)}
    out = _eval(samples, {1: 10, 2: 10}, store_open={10: True},
                overrides={1: {"enabled": False}})
    # Cam 1 gone from surge AND from the store sum (20 < 30).
    assert all(t["camera_id"] != 1 for t in out)
    assert all(t["rule"] != "store_surge" for t in out)


def test_override_thresholds_apply_per_camera() -> None:
    samples = {1: _win(9, 9), 2: _win(9, 9)}
    out = _eval(samples, {1: 10, 2: 10}, store_open={10: True},
                overrides={1: {"enabled": True, "surge_people": 8,
                               "surge_sustain_samples": 2}})
    hits = [t for t in out if t["rule"] == "occupancy_surge"]
    assert [t["camera_id"] for t in hits] == [1]   # cam 2 keeps defaults


def test_opt_out_only_explicit_false_disables() -> None:
    samples = {1: _win(13, 13, 13), 2: _win(13, 13, 13), 3: _win(13, 13, 13)}
    out = _eval(samples, {1: 10, 2: 10, 3: 10}, store_open={10: True},
                overrides={
                    1: {"surge_people": 12},        # threshold-only row
                    2: {"enabled": None},           # no explicit flag
                    # camera 3: no row at all
                })
    fired = sorted(t["camera_id"] for t in out
                   if t["rule"] == "occupancy_surge")
    assert fired == [1, 2, 3]                       # all evaluated
    out = _eval(samples, {1: 10, 2: 10, 3: 10}, store_open={10: True},
                overrides={2: {"enabled": False}})  # explicit opt-out
    fired = sorted(t["camera_id"] for t in out
                   if t["rule"] == "occupancy_surge")
    assert fired == [1, 3]


def test_malformed_override_values_fall_back_to_defaults() -> None:
    out = _eval({1: _win(13, 13, 13)}, {1: 10}, store_open={10: True},
                overrides={1: {"enabled": True, "surge_people": "lots"}})
    assert any(t["rule"] == "occupancy_surge" for t in out)   # default 12


# ── output contract ────────────────────────────────────────────────────────

def test_deterministic_ordering_and_shape() -> None:
    samples = {2: _win(13, 13, 13), 1: _win(13, 13, 13),
               3: _win(0, 0, 4)}
    out = _eval(samples, {1: 10, 2: 10, 3: 11},
                store_open={10: True, 11: False})
    assert [ (t["rule"], t["camera_id"]) for t in out ] == sorted(
        [(t["rule"], t["camera_id"]) for t in out])
    for t in out:
        assert set(t) == {"rule", "camera_id", "store_id",
                          "severity", "extra"}


def test_empty_inputs_no_triggers() -> None:
    assert _eval({}, {}) == []
    assert _eval({1: []}, {1: 10}, store_open={10: True}) == []
