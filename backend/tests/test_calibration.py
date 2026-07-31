"""Tests for app.ai.calibration.effective_threshold — the dynamic
per-zone / per-time-band confidence calibration layer."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("zoneinfo")

from app.ai.calibration import EAT, effective_threshold  # noqa: E402


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 7, 31, hh, mm, tzinfo=EAT)


def test_base_threshold_passthrough() -> None:
    assert effective_threshold({"confidence_threshold": 0.6}) == 0.6


def test_default_when_unconfigured() -> None:
    assert effective_threshold(None, default=0.45) == 0.45
    assert effective_threshold({}, default=0.45) == 0.45


def test_zone_override_replaces_base() -> None:
    cfg = {"confidence_threshold": 0.5,
           "extra": {"zone_conf_overrides": {"12": 0.7}}}
    assert effective_threshold(cfg, zone_id=12) == 0.7
    assert effective_threshold(cfg, zone_id=99) == 0.5     # no override


def test_zone_override_accepts_int_keys() -> None:
    cfg = {"confidence_threshold": 0.5,
           "extra": {"zone_conf_overrides": {14: 0.4}}}
    assert effective_threshold(cfg, zone_id=14) == 0.4


def test_time_band_multiplier_applies_inside_band() -> None:
    cfg = {"confidence_threshold": 0.6,
           "extra": {"conf_time_bands": [
               {"start": "07:00", "end": "09:00", "multiplier": 0.5}]}}
    assert effective_threshold(cfg, now_eat=_at(7, 30)) == pytest.approx(0.3)
    assert effective_threshold(cfg, now_eat=_at(12, 0)) == 0.6   # outside


def test_band_boundaries_start_inclusive_end_exclusive() -> None:
    cfg = {"confidence_threshold": 0.6,
           "extra": {"conf_time_bands": [
               {"start": "07:00", "end": "09:00", "multiplier": 0.5}]}}
    assert effective_threshold(cfg, now_eat=_at(7, 0)) == pytest.approx(0.3)
    assert effective_threshold(cfg, now_eat=_at(9, 0)) == 0.6


def test_zone_override_then_band_compose() -> None:
    cfg = {"confidence_threshold": 0.5,
           "extra": {"zone_conf_overrides": {"3": 0.8},
                     "conf_time_bands": [
                         {"start": "18:00", "end": "20:00",
                          "multiplier": 0.5}]}}
    assert effective_threshold(cfg, zone_id=3,
                               now_eat=_at(19, 0)) == pytest.approx(0.4)


def test_clamped_to_sane_range() -> None:
    cfg = {"confidence_threshold": 0.9,
           "extra": {"conf_time_bands": [
               {"start": "00:00", "end": "23:59", "multiplier": 5.0}]}}
    assert effective_threshold(cfg, now_eat=_at(12, 0)) == 0.99
    cfg2 = {"confidence_threshold": 0.1,
            "extra": {"conf_time_bands": [
                {"start": "00:00", "end": "23:59", "multiplier": 0.01}]}}
    assert effective_threshold(cfg2, now_eat=_at(12, 0)) == 0.05


def test_malformed_entries_degrade_to_base() -> None:
    cfg = {"confidence_threshold": 0.55,
           "extra": {"zone_conf_overrides": {"7": "not-a-number"},
                     "conf_time_bands": [
                         {"start": "bogus", "end": "09:00", "multiplier": 0.5},
                         "not-even-a-dict"]}}
    assert effective_threshold(cfg, zone_id=7, now_eat=_at(8, 0)) == 0.55
