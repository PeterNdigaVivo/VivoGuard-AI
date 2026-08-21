"""Regression tests for concurrent uniform dwell/violation timing."""
from types import SimpleNamespace

from app.ai.detectors.uniform_compliance import (
    NON_COMPLIANT, UniformComplianceDetector,
)


def test_violation_timer_can_run_during_staff_confirmation() -> None:
    detector = UniformComplianceDetector()
    detector._observe_state(12, NON_COMPLIANT, 1700.0)
    ctx = SimpleNamespace(store_id=4)
    det = {"bbox_norm": [0.1, 0.1, 0.3, 0.8]}

    event = detector._maybe_alert(ctx, det, 12, NON_COMPLIANT, 2000.0)

    assert event is not None
    assert event.cls == NON_COMPLIANT
    assert event.extra["rule"] == "uniform_violation"


def test_uniform_state_change_resets_sustained_timer() -> None:
    detector = UniformComplianceDetector()
    assert detector._observe_state(7, NON_COMPLIANT, 10.0) == 0.0
    assert detector._observe_state(7, NON_COMPLIANT, 20.0) == 10.0
    assert detector._observe_state(7, "full_compliant", 21.0) == 0.0
