"""Tests for app.ai.model_gating — the pre-deployment validation gate.

`ultralytics` is not installed in CI, so a fake module is injected into
sys.modules; the gate's contract (class-map exact match, dummy-inference
smoke test, never-raises semantics) is what's under test.
"""
from __future__ import annotations

import sys
import types

import pytest

np = pytest.importorskip("numpy")

from app.ai.model_gating import (        # noqa: E402
    check_metric_regression, validate_model_before_deploy,
)


class FakeYOLO:
    """Stands in for ultralytics.YOLO."""
    names: dict[int, str] = {0: "person", 1: "vehicle"}
    load_error: Exception | None = None
    predict_error: Exception | None = None

    def __init__(self, path: str) -> None:
        if type(self).load_error is not None:
            raise type(self).load_error
        self.path = path
        import copy
        self.names = copy.copy(type(self).names)

    def predict(self, img, **kw):
        if type(self).predict_error is not None:
            raise type(self).predict_error
        return []


@pytest.fixture(autouse=True)
def fake_ultralytics(monkeypatch: pytest.MonkeyPatch):
    FakeYOLO.names = {0: "person", 1: "vehicle"}
    FakeYOLO.load_error = None
    FakeYOLO.predict_error = None
    monkeypatch.setitem(sys.modules, "ultralytics",
                        types.SimpleNamespace(YOLO=FakeYOLO))
    yield


def test_pass_when_classes_match() -> None:
    assert validate_model_before_deploy("w.pt", ["person", "vehicle"]) is True


def test_reject_on_class_mismatch() -> None:
    assert validate_model_before_deploy("w.pt", ["person"]) is False
    assert validate_model_before_deploy("w.pt", ["vehicle", "person"]) is False


def test_empty_required_skips_class_check_but_smoke_tests() -> None:
    assert validate_model_before_deploy("w.pt", []) is True
    FakeYOLO.predict_error = RuntimeError("CUDA error: device-side assert")
    assert validate_model_before_deploy("w.pt", []) is False


def test_reject_when_weights_fail_to_load() -> None:
    FakeYOLO.load_error = OSError("corrupt weights file")
    assert validate_model_before_deploy("w.pt", ["person", "vehicle"]) is False


def test_reject_when_inference_crashes_and_never_raises() -> None:
    FakeYOLO.predict_error = RuntimeError("boom")
    # Contract: returns False, never propagates.
    assert validate_model_before_deploy("w.pt", ["person", "vehicle"]) is False


def test_list_style_names_accepted() -> None:
    FakeYOLO.names = ["person", "vehicle"]          # older ultralytics style
    assert validate_model_before_deploy("w.pt", ["person", "vehicle"]) is True


# ── check_metric_regression ────────────────────────────────────────────────

def test_regression_strict_default() -> None:
    ok, _ = check_metric_regression(0.80, 0.81)
    assert ok is False                              # any drop fails at 0.0
    ok, _ = check_metric_regression(0.81, 0.81)
    assert ok is True


def test_regression_with_tolerance() -> None:
    ok, _ = check_metric_regression(0.77, 0.81, max_regression=0.05)
    assert ok is True                               # within 5 points
    ok, reason = check_metric_regression(0.75, 0.81, max_regression=0.05)
    assert ok is False
    assert "0.7500" in reason


def test_regression_missing_metrics_pass() -> None:
    assert check_metric_regression(None, 0.8)[0] is True
    assert check_metric_regression(0.8, None)[0] is True
