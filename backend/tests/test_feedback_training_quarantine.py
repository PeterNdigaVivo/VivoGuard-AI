"""Provenance policy for operator feedback.

The dual-review quarantine (codex data-integrity work) is now a policy
switch: settings.training_require_dual_review. Default False — Vivo
runs a single-reviewer workflow, and the always-on quarantine starved
training to zero (cross-store jobs 807/808/809, pseudo-labeler
labelled=0). These tests pin BOTH sides of the switch.
"""
from app.config import settings
from app.training.feedback_loop import _training_provenance


def test_dual_review_gate_quarantines_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "training_require_dual_review", True)
    dismissed = _training_provenance("false")
    assert dismissed == {
        "source_kind": "operator_dismissed",
        "eligible_for_training": False,
        "review_state": "pending",
    }
    confirmed = _training_provenance("correct")
    assert confirmed["source_kind"] == "operator_confirmed"
    assert confirmed["eligible_for_training"] is False
    assert confirmed["review_state"] == "pending"


def test_single_reviewer_policy_trains_immediately_by_default(monkeypatch):
    monkeypatch.setattr(settings, "training_require_dual_review", False)
    confirmed = _training_provenance("correct")
    assert confirmed["source_kind"] == "operator_confirmed"
    assert confirmed["eligible_for_training"] is True
    assert confirmed["review_state"] == "approved"
    dismissed = _training_provenance("false")
    assert dismissed["source_kind"] == "operator_dismissed"
    assert dismissed["eligible_for_training"] is True
    assert dismissed["review_state"] == "approved"
