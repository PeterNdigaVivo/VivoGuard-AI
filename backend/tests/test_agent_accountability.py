from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent_control.accountability import build_scorecard


def _report(at, *, status="ok", error=None, findings=None):
    return SimpleNamespace(run_at=at, status=status, error_message=error,
                           findings=findings if findings is not None else {"checked": 1})


def test_domain_critical_is_not_misclassified_as_agent_sla_failure():
    now = datetime.now(timezone.utc)
    reports = [_report(now - timedelta(minutes=2), status="critical")]
    card = build_scorecard("streamer", reports, now=now, window_seconds=300)
    assert card["expected_runs"] == 1
    assert card["completed_runs"] == 1
    assert card["domain_critical"] is True
    assert card["active_critical_override"] is False
    assert card["compliant"] is True
    assert "latest_run_critical" not in card["breaches"]


def test_scorecard_fails_stale_or_missing_run_evidence():
    now = datetime.now(timezone.utc)
    card = build_scorecard("streamer", [], now=now, window_seconds=3600)
    assert card["expected_runs"] == 12
    assert card["run_coverage"] == 0
    assert "latest_evidence_stale" in card["breaches"]
    assert card["measurement_limitations"]


def test_new_agent_is_measured_only_since_first_observed_run():
    now = datetime.now(timezone.utc)
    first = now - timedelta(minutes=11)
    reports = [_report(first), _report(now - timedelta(minutes=6)),
               _report(now - timedelta(minutes=1))]
    card = build_scorecard(
        "streamer", reports, now=now, window_seconds=24 * 3600,
        observation_started_at=first,
    )
    assert card["measurement_warmup"] is True
    assert card["expected_runs"] == 3
    assert card["run_coverage"] == 1
    assert card["compliant"] is True


def test_partial_probe_errors_fail_output_quality_sla():
    now = datetime.now(timezone.utc)
    reports = [_report(now - timedelta(minutes=2),
                       findings={"db": "ok", "_errors": {"inference": "bad payload"}})]
    card = build_scorecard("streamer", reports, now=now, window_seconds=300)
    assert card["valid_outputs"] == 0
    assert card["compliant"] is False
    assert "valid_output_below_99pct" in card["breaches"]
