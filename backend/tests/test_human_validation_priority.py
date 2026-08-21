from app.api.labels import _review_priority


def test_life_safety_alerts_are_reviewed_before_operational_and_routine():
    assert _review_priority("weapon")[0] < _review_priority("intrusion")[0]
    assert _review_priority("intrusion")[0] < _review_priority("crowd")[0]


def test_staff_area_and_shrinkage_are_high_risk_not_accusatory_truth():
    assert _review_priority("staff_zone") == (1, "high-risk operational review")
    assert _review_priority("shrinkage") == (1, "high-risk operational review")
