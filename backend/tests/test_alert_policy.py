"""Retail alert policy: one decision, with a recorded reason, per detection."""
from app.services.alert_policy import (
    DISCARDED, INCIDENT, LOW, METRIC_ONLY, REVIEW, URGENT, PolicyInput, decide,
)

STOCKROOM = frozenset({"restricted"})


def _d(dtype, **kw):
    return decide(PolicyInput(detection_type=dtype, **kw))


# --- person before hours -------------------------------------------------

def test_before_hours_intrusion_needs_zone_schedule_and_frames():
    d = _d("intrusion", extra={"time_context": "before_hours", "staff_level": "unknown"},
           zone_id=7, zone_tags=STOCKROOM, frames_seen=6, min_frames=4)
    assert (d.decision, d.priority) == (INCIDENT, URGENT)
    assert d.reason == "confirmed_before_hours_intrusion"


def test_before_hours_whole_frame_default_is_review_not_urgent():
    d = _d("intrusion", extra={"time_context": "before_hours"},
           zone_id=None, frames_seen=6)
    assert d.decision == REVIEW and d.reason == "before_hours_no_protected_zone"


def test_before_hours_without_schedule_is_review():
    d = _d("intrusion", extra={"time_context": "before_hours"}, zone_id=7,
           zone_tags=STOCKROOM, frames_seen=6, has_schedule=False)
    assert d.decision == REVIEW and d.reason == "before_hours_no_store_schedule"


def test_before_hours_single_frame_is_discarded():
    d = _d("intrusion", extra={"time_context": "before_hours"}, zone_id=7,
           zone_tags=STOCKROOM, frames_seen=1, min_frames=4)
    assert d.decision == DISCARDED and d.reason == "not_confirmed_across_frames"


def test_expected_early_staff_is_low_priority_review():
    d = _d("intrusion", extra={"time_context": "before_hours", "staff_level": "high"},
           zone_id=7, zone_tags=STOCKROOM, frames_seen=8)
    assert (d.decision, d.priority, d.reason) == (REVIEW, LOW, "expected_early_staff")


def test_confirmed_after_hours_intrusion_stays_urgent():
    d = _d("intrusion", extra={"time_context": "after_hours", "after_hours": True},
           frames_seen=5)
    assert (d.decision, d.priority) == (INCIDENT, URGENT)


def test_static_fixture_never_becomes_intrusion():
    d = _d("intrusion", extra={"time_context": "after_hours", "static": True},
           frames_seen=50)
    assert d.decision == DISCARDED


# --- staff-only zones ----------------------------------------------------

def test_staff_zone_requires_configured_polygon():
    assert _d("staff_zone", zone_id=None).reason == "no_configured_zone"


def test_staff_zone_person_outside_polygon_is_discarded():
    d = _d("staff_zone", zone_id=3, extra={"in_zone": False})
    assert d.decision == DISCARDED and d.reason == "person_outside_zone"


def test_staff_zone_confirmed_person_is_incident():
    d = _d("staff_zone", zone_id=3, frames_seen=5,
           extra={"rule": "unauthorised_person", "priority": "high",
                  "identity_level": "low"})
    assert (d.decision, d.priority) == (INCIDENT, URGENT)


def test_staff_zone_recognised_staff_is_review():
    d = _d("staff_zone", zone_id=3, extra={"identity_level": "medium"})
    assert d.decision == REVIEW


# --- store opening --------------------------------------------------------

def test_store_not_opened_requires_schedule():
    ok = _d("shop_open_close", extra={"rule": "shop_not_opened"})
    assert ok.decision == INCIDENT
    no_sched = _d("shop_open_close", extra={"rule": "shop_not_opened"},
                  has_schedule=False)
    assert no_sched.decision == REVIEW


def test_store_opened_is_not_an_incident():
    d = _d("shop_open_close", extra={"rule": "shop_opened"})
    assert d.decision == METRIC_ONLY


# --- noisy detectors default to review ------------------------------------

def test_generic_detectors_are_review_only_until_validated():
    for dtype in ("scene_review", "shelf_change", "audio_distress", "shrinkage"):
        d = _d(dtype, extra={"priority": "high"})
        assert d.decision == REVIEW, dtype
        assert d.reason == "review_only_until_validated"
    validated = _d("audio_distress", validated_types=frozenset({"audio_distress"}))
    assert validated.decision == INCIDENT


# --- safety critical ------------------------------------------------------

def test_safety_critical_kept_but_needs_evidence():
    assert _d("fire", confidence=0.9).decision == INCIDENT
    assert _d("fire", confidence=0.9).priority == URGENT
    weak = _d("weapon", confidence=0.4)
    assert weak.decision == REVIEW and weak.reason == "safety_low_confidence"
    blip = _d("fight", confidence=0.9, frames_seen=1, min_frames=4)
    assert blip.decision == REVIEW


# --- coverage, metrics, suppression ---------------------------------------

def test_camera_health_is_coverage_not_incident():
    assert _d("system_health").reason == "coverage_issue"
    assert _d("system_health").decision == METRIC_ONLY


def test_metric_and_informational_types_never_incident():
    assert _d("heatmap").decision == METRIC_ONLY
    assert _d("store_intelligence").decision == METRIC_ONLY


def test_low_confidence_is_discarded():
    assert _d("loitering", confidence=0.1).reason == "low_confidence"


def test_suppressed_detection_keeps_its_reason():
    d = _d("loitering", suppressed_reason="not_confirmed_across_frames")
    assert d.decision == DISCARDED and d.reason == "not_confirmed_across_frames"
    raw = _d("loitering", suppressed_reason="not_confirmed_across_frames",
             raw_review=True)
    assert raw.decision == REVIEW


def test_decision_is_serialisable_for_audit():
    out = _d("fire", confidence=0.9).as_dict()
    assert set(out) == {"decision", "reason", "priority", "version"}
