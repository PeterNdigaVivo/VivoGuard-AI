from app.ai.inference_worker import _alert_disposition


def test_metric_events_are_not_alerts() -> None:
    assert _alert_disposition("entry_exit", False) == "metric_only"
    assert _alert_disposition("occupancy_metrics", False) == "metric_only"


def test_filtered_events_are_distinct_from_alerts() -> None:
    assert _alert_disposition("person", True) == "filtered"
    assert _alert_disposition("uniform_compliance", False) == "alert"
