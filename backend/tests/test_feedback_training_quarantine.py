from app.training.feedback_loop import _training_provenance


def test_single_reviewer_dismissal_is_quarantined_from_training():
    state = _training_provenance("false")

    assert state == {
        "source_kind": "operator_dismissed",
        "eligible_for_training": False,
        "review_state": "pending",
    }


def test_confirmed_feedback_keeps_existing_approved_provenance():
    state = _training_provenance("correct")

    assert state["source_kind"] == "operator_verified"
    assert state["eligible_for_training"] is True
    assert state["review_state"] == "approved"
