from app.training.feedback_loop import _training_provenance


def test_single_reviewer_dismissal_is_quarantined_from_training():
    state = _training_provenance("false")

    assert state == {
        "source_kind": "operator_dismissed",
        "eligible_for_training": False,
        "review_state": "pending",
    }


def test_single_reviewer_confirmation_is_quarantined_from_training():
    state = _training_provenance("correct")

    assert state["source_kind"] == "operator_confirmed"
    assert state["eligible_for_training"] is False
    assert state["review_state"] == "pending"
