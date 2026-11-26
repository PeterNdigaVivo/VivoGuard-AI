"""Provenance policy for operator feedback.

Nov 2026: an operator click is review EVIDENCE, never a training label.
Every verdict lands in the evidence queue (pending_review, or excluded
when it can never be trainable) and no verdict triggers a retrain. The
old training_require_dual_review switch no longer makes a click
trainable either way. See app.services.training_evidence.
"""
import pytest

from app.config import settings
from app.training.feedback_loop import _training_provenance


@pytest.mark.parametrize("dual_review", [False, True])
def test_no_verdict_is_trainable_on_click(monkeypatch, dual_review):
    monkeypatch.setattr(settings, "training_require_dual_review", dual_review)
    confirmed = _training_provenance("correct", "person")
    assert confirmed == {
        "source_kind": "operator_confirmed",
        "eligible_for_training": False,
        "review_state": "pending_review",
        "example_kind": "positive",
        "target_class": "person",
    }
    dismissed = _training_provenance("false", "intrusion", "not_a_person")
    assert dismissed["eligible_for_training"] is False
    assert dismissed["example_kind"] == "negative"
    assert dismissed["target_class"] == "person"


def test_staff_dismissal_is_a_policy_error_not_background():
    p = _training_provenance("false", "intrusion", "staff_member")
    assert p["example_kind"] == "policy_error"
    assert p["review_state"] == "excluded"
    assert p["target_class"] is None


def test_absorbing_a_verdict_never_queues_training(monkeypatch):
    """The click path no longer calls the retrain trigger at all."""
    import inspect

    import app.training.feedback_loop as fl
    for fn in (fl.absorb_confirmed, fl.absorb_dismissed):
        src = inspect.getsource(fn)
        assert "_enqueue_if_trainable" not in src
        assert "_maybe_enqueue_training" not in src
