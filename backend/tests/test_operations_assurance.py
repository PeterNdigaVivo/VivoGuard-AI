from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.odoo_pos import normalise_odoo_event
from app.models import AssuranceCase
from app.operations.assurance import (
    assess_coverage, create_alert_quality_cases, create_lone_worker_cases, risk_band,
    score_operational_event, store_is_open,
)


def test_pos_score_is_transparent_and_review_only():
    score, factors = score_operational_event("no_sale", 75_000,
                                             after_hours=True, camera_evidence=False)
    assert score == 0.9
    assert risk_band(score) == "high_review"
    assert {f["signal"] for f in factors} == {
        "no_sale", "high_value_amount", "outside_business_hours", "camera_evidence_unavailable"}


def test_store_hours_are_timezone_aware():
    store = SimpleNamespace(timezone="Africa/Nairobi",
                            business_hours_json={"fri": ["09:00-20:00"]})
    assert store_is_open(store, datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc))
    assert not store_is_open(store, datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc))


def test_odoo_normaliser_minimises_payload():
    result = normalise_odoo_event({"event": "pos.refund", "id": 42, "store_id": 3,
                                   "occurred_at": "2026-08-21T10:00:00Z", "amount_total": 2500,
                                   "employee_id": "EMP-7", "customer_name": "must-not-pass"})
    assert result["event_type"] == "refund"
    assert result["source_event_id"] == "42"
    assert "customer_name" not in result["payload"]


def test_assurance_agents_resolve_cases_when_conditions_clear():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        coverage = AssuranceCase(
            dedup_key="coverage-config:999", case_type="coverage_gap",
            severity="critical", status="open", title="Old coverage gap",
        )
        lone_worker = AssuranceCase(
            dedup_key="lone-worker:999:2026-08-21", case_type="lone_worker",
            severity="high", status="open", title="Old lone-worker review",
        )
        alert_quality = AssuranceCase(
            dedup_key="alert-quality:999:evidence_missing", case_type="alert_quality",
            severity="high", status="open", title="Old alert-quality exception",
        )
        db.add_all([coverage, lone_worker, alert_quality])
        db.commit()

        assess_coverage(db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
                        persist=True)
        create_lone_worker_cases(
            db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
        create_alert_quality_cases(
            db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))

        assert coverage.status == "resolved"
        assert coverage.resolved_at is not None
        assert lone_worker.status == "resolved"
        assert lone_worker.resolved_at is not None
        assert alert_quality.status == "resolved"
        assert alert_quality.resolved_at is not None
