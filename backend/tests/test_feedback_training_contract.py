"""Safety contract for routing alert feedback into object detection."""
from app.training.feedback_loop import direct_yolo_feedback_label


def test_raw_person_feedback_uses_canonical_person_class() -> None:
    assert direct_yolo_feedback_label("person") == "person"
    assert direct_yolo_feedback_label("live_activity") == "person"


def test_semantic_alerts_never_become_yolo_classes_or_backgrounds() -> None:
    for detection_type in (
        "staff_present",
        "uniform_compliance",
        "checkout_dwell",
        "shop_open_close",
        "intrusion",
        "trespass",
        "abandoned_object",
    ):
        assert direct_yolo_feedback_label(detection_type) is None
