"""Scoring rules for the model-evaluation report (app/eval/scoring.py)."""
import pytest

from app.eval import scoring
from app.eval.categories import COUNTER_UNATTENDED, PERSON, UNIFORM, eval_category

COUNTER = [[0.0, 0.5], [0.5, 0.5], [0.5, 1.0], [0.0, 1.0]]      # bottom-left quarter


def det(box, cls="person", conf=0.9):
    return {"cls": cls, "conf": conf, "bbox_norm": box}


def test_eval_category_mapping():
    assert eval_category("staff_present", "counter_unattended") == COUNTER_UNATTENDED
    assert eval_category("staff_present", "something_else") is None
    for t in ("person", "intrusion", "trespass"):
        assert eval_category(t) == PERSON
    assert eval_category("uniform_compliance") == UNIFORM
    assert eval_category("fire") is None


def test_person_boxes_filters_class_and_confidence():
    dets = [det([0, 0, .1, .1]), det([0, 0, .2, .2], conf=0.1), det([0, 0, .3, .3], cls="chair")]
    assert scoring.person_boxes(dets, 0.25) == [[0, 0, .1, .1]]


def test_person_in_zone_generous_vs_centre():
    leaning = [0.4, 0.3, 0.8, 0.6]          # overlaps the counter, centre outside it
    assert scoring.person_in_zone([leaning], COUNTER, generous=True)
    assert not scoring.person_in_zone([leaning], COUNTER, generous=False)
    assert scoring.person_in_zone([[0.6, 0.0, 0.7, 0.1]], None, generous=False)   # whole frame
    assert not scoring.person_in_zone([], None, generous=True)


@pytest.mark.parametrize("category, kwargs, expected", [
    (COUNTER_UNATTENDED, {"person_present": False}, True),     # nobody at counter -> alert
    (COUNTER_UNATTENDED, {"person_present": True}, False),
    (PERSON, {"person_present": True}, True),
    (PERSON, {"person_present": False}, False),
    (UNIFORM, {"uniform_state": "non_compliant"}, True),
    (UNIFORM, {"uniform_state": "color_only"}, True),
    (UNIFORM, {"uniform_state": "full_compliant"}, False),
    (UNIFORM, {"uniform_state": "uncertain"}, False),
])
def test_model_raises_alert(category, kwargs, expected):
    assert scoring.model_raises_alert(category, **kwargs) is expected


def test_alert_outcome_buckets():
    assert scoring.alert_outcome("true", True) == "real_alert_kept"
    assert scoring.alert_outcome("true", False) == "real_alert_missed"
    assert scoring.alert_outcome("false", False) == "false_alert_avoided"
    assert scoring.alert_outcome("false", True) == "false_alert_repeated"
    with pytest.raises(ValueError):
        scoring.alert_outcome("maybe", True)


def test_match_clicks_found_missed_false_and_edge():
    boxes = [[0.10, 0.10, 0.30, 0.50],      # contains click A
             [0.12, 0.12, 0.20, 0.30],      # smaller, also contains A -> A claims this one
             [0.70, 0.70, 0.80, 0.90]]      # nobody clicked here -> false detection
    points = [[0.15, 0.20],                 # A
              [0.25, 0.40],                 # B: only inside the big box
              [0.97, 0.50]]                 # C: missed, at the right edge
    m = scoring.match_clicks(boxes, points)
    assert m == {"found": 2, "missed": 1, "false": 1, "edge_total": 1, "edge_found": 0}


def test_each_box_counts_once():
    m = scoring.match_clicks([[0, 0, 1, 1]], [[0.4, 0.4], [0.6, 0.6]])
    assert (m["found"], m["missed"], m["false"]) == (1, 1, 0)


def test_summaries():
    s = scoring.summarise_alerts([
        (PERSON, "real_alert_kept"), (PERSON, "real_alert_missed"),
        (PERSON, "false_alert_avoided"), (PERSON, "false_alert_avoided"),
    ])[PERSON]
    assert s["samples"] == 4
    assert s["real_alerts_kept_pct"] == 50.0
    assert s["false_alerts_avoided_pct"] == 100.0
    assert s["accuracy_pct"] == 75.0
    p = scoring.summarise_people([
        {"found": 3, "missed": 1, "false": 1, "edge_total": 2, "edge_found": 1},
        {"found": 1, "missed": 0, "false": 0, "edge_total": 0, "edge_found": 0},
    ])
    assert p["people_labelled"] == 5 and p["found_pct"] == 80.0
    assert p["precision_pct"] == 80.0 and p["edge_found_pct"] == 50.0
    sp = scoring.summarise_speed([5.0, 1.0, 3.0, 2.0, 4.0])
    assert sp == {"frames": 5, "median_ms": 3.0, "p95_ms": 5.0}
    assert scoring.summarise_speed([])["median_ms"] is None
