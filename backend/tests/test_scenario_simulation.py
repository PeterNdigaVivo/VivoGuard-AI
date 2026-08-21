from app.simulation.catalog import SCENARIOS
from app.simulation.runner import evaluate, missing_feedback_fields, run_catalog


def test_full_scenario_catalog_passes_in_isolation():
    result = run_catalog()
    assert result["failed"] == 0
    assert result["passed"] == len(SCENARIOS)
    assert result["execution_mode"] == "isolated_simulation"
    assert result["training_eligible"] is False
    assert result["production_alerts_created"] == 0


def test_ambiguous_human_feedback_is_never_training_eligible():
    inputs = {"store": None, "camera": None, "occurred_at": None,
              "observed": "yes", "expected": None}
    result = evaluate("feedback", inputs)
    assert result["clarification_required"] is True
    assert result["training_eligible"] is False
    assert set(missing_feedback_fields(inputs)) == {
        "store", "camera", "occurred_at", "observed", "expected"}


def test_simulation_runner_has_no_live_model_or_notification_imports():
    import inspect
    import app.simulation.runner as runner

    source = inspect.getsource(runner)
    for forbidden in ("DetectionEvent", "TrainingImage", "_send_whatsapp", "_persist_event"):
        assert forbidden not in source
