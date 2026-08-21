from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent_control.accountability import build_scorecard


def _report(at, *, status="ok", error=None, findings=None):
    return SimpleNamespace(run_at=at, status=status, error_message=error,
                           findings=findings if findings is not None else {"checked": 1})


def test_scorecard_exposes_numerators_and_active_critical_override():
    now = datetime.now(timezone.utc)
    reports = [_report(now - timedelta(minutes=2), status="critical")]
    card = build_scorecard("streamer", reports, now=now, window_seconds=300)
    assert card["expected_runs"] == 1
    assert card["completed_runs"] == 1
    assert card["active_critical_override"] is True
    assert card["compliant"] is False
    assert "latest_run_critical" in card["breaches"]


def test_scorecard_fails_stale_or_missing_run_evidence():
    now = datetime.now(timezone.utc)
    card = build_scorecard("streamer", [], now=now, window_seconds=3600)
    assert card["expected_runs"] == 12
    assert card["run_coverage"] == 0
    assert "latest_evidence_stale" in card["breaches"]
    assert card["measurement_limitations"]
