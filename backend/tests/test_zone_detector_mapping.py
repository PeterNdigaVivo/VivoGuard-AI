from app.zone_purposes import detector_types_for_zone_tags


def test_counter_zone_activates_counter_workflows():
    assert detector_types_for_zone_tags(["counter"]) == {
        "staff_present", "checkout_dwell", "uniform_compliance",
    }


def test_retail_zone_tags_activate_executable_detectors():
    assert detector_types_for_zone_tags({
        "entry_exit", "restricted", "aisle", "high_value",
    }) == {"entry_exit", "intrusion", "dwell", "shrinkage"}


def test_modifier_tags_do_not_create_fake_detectors():
    assert detector_types_for_zone_tags({
        "entry_exit", "glass_door", "changing_room",
    }) == {"entry_exit"}


def test_unknown_tag_passes_through_for_future_detectors():
    assert detector_types_for_zone_tags(["future_detector"]) == {
        "future_detector",
    }
