from app.simulation.catalog import SCENARIOS
from app.simulation.runner import evaluate, missing_feedback_fields, run_catalog


def test_full_scenario_catalog_passes_in_isolation():
    result = run_catalog()
    assert result["failed"] == 0
    assert result["passed"] == len(SCENARIOS)
    assert result["execution_mode"] == "isolated_simulation"
    assert result["training_eligible"] is False
    assert result["production_alerts_created"] == 0
    assert result["notifications_sent"] == 0
    assert result["training_samples_created"] == 0
    assert result["release_gate"]["passed"] is True
    assert result["action_metrics"]["false_positive"] == 0
    assert result["action_metrics"]["false_negative"] == 0
    assert result["action_metrics"]["precision"] == 1.0
    assert result["action_metrics"]["recall"] == 1.0


def test_ambiguous_human_feedback_is_never_training_eligible():
    inputs = {"store": None, "camera": None, "occurred_at": None,
              "observed": "yes", "expected": None}
    result = evaluate("feedback", inputs)
    assert result["clarification_required"] is True
    assert result["training_eligible"] is False
    assert set(missing_feedback_fields(inputs)) == {
        "store", "camera", "occurred_at", "observed", "expected", "label"}


def test_requested_monday_domains_have_positive_and_negative_scenarios():
    domains = {scenario["domain"] for scenario in SCENARIOS}
    assert {"identity_handoff", "merchandise_flow", "zone_boundary",
            "camera_health", "alert_pipeline", "lone_worker",
            "pos_correlation", "human_feedback"}.issubset(domains)
    ids = {scenario["id"] for scenario in SCENARIOS}
    assert len(ids) == len(SCENARIOS)
    assert len(SCENARIOS) >= 25


def test_zone_boundary_requires_spatial_and_temporal_confirmation():
    assert evaluate("zone_boundary", {
        "inside_ratio": 0.9, "consecutive_frames": 1,
        "min_inside_ratio": 0.6, "min_frames": 3,
    })["alert"] is False
    assert evaluate("zone_boundary", {
        "inside_ratio": 0.9, "consecutive_frames": 3,
        "min_inside_ratio": 0.6, "min_frames": 3,
    })["alert"] is True


def test_handoff_id_loss_never_claims_a_confident_unique_count():
    result = evaluate("camera_handoff", {
        "expected_same_person": True, "source_global_id": "person-7",
        "target_global_id": None, "gap_seconds": 3, "max_gap_seconds": 15,
    })
    assert result == {"identity_preserved": False, "id_loss": True,
                      "review": True, "unique_count_confident": False}


def test_merchandise_mismatch_is_review_not_accusation():
    result = evaluate("merchandise_flow", {
        "outbound_qty": 3, "sold_qty": 1, "transfer_qty": 0,
        "return_qty": 0, "camera_evidence": True,
    })
    assert result["unmatched_qty"] == 2
    assert result["review"] is True
    assert result["accusation"] is False


def test_probabilistic_human_label_requires_clarification():
    result = evaluate("feedback", {
        "store": "Junction", "camera": "Channel 5",
        "occurred_at": "2026-08-21T13:40:00+03:00",
        "observed": "person behind counter", "expected": "staff only",
        "label": "probably false",
    })
    assert result["clarification_required"] is True
    assert result["label_ready"] is False
    assert result["training_eligible"] is False


def test_simulation_runner_has_no_live_model_or_notification_imports():
    import inspect
    import app.simulation.runner as runner

    source = inspect.getsource(runner)
    for forbidden in ("DetectionEvent", "TrainingImage", "_send_whatsapp",
                      "_persist_event", "SessionLocal", "Alert("):
        assert forbidden not in source
