from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agent_control.workstreams import (
    MONDAY_DEADLINE, MONDAY_WORKSTREAMS, evaluate_workstream,
    reconcile_workstream_case, workstream_statuses,
)
from app.database import Base
from app.models import AssuranceCase


def _evidence(name: str, value: bool) -> dict:
    criteria = MONDAY_WORKSTREAMS[name]["completion_criteria"]
    return {"checks": {criterion: value for criterion in criteria}}


def test_all_four_workstreams_have_objective_accountability_contracts():
    assert set(MONDAY_WORKSTREAMS) == {
        "deployment_reliability", "cctv_engineering",
        "simulation_evaluation", "human_validation",
    }
    for definition in MONDAY_WORKSTREAMS.values():
        assert definition["owner"]
        assert definition["deliverables"]
        assert definition["sla"]["review_cadence_seconds"] == 300
        assert definition["evidence_sources"]
        assert definition["completion_criteria"]


def test_completion_requires_every_objective_check():
    now = MONDAY_DEADLINE - timedelta(days=1)
    complete = evaluate_workstream(
        "simulation_evaluation", _evidence("simulation_evaluation", True), now=now)
    assert complete["complete"] is True
    assert complete["status"] == "complete"
    assert complete["completion_percentage"] == 100

    evidence = _evidence("simulation_evaluation", True)
    evidence["checks"]["all_catalog_scenarios_pass"] = False
    incomplete = evaluate_workstream("simulation_evaluation", evidence, now=now)
    assert incomplete["status"] == "at_risk"
    assert incomplete["breaches"] == ["all_catalog_scenarios_pass"]


def test_incomplete_workstream_becomes_overdue_after_monday_deadline():
    result = evaluate_workstream(
        "human_validation", _evidence("human_validation", False),
        now=MONDAY_DEADLINE + timedelta(seconds=1))
    assert result["status"] == "overdue"
    assert result["deadline_breached"] is True


def test_assurance_case_opens_resolves_and_reopens_on_regression():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        incomplete = evaluate_workstream(
            "deployment_reliability", _evidence("deployment_reliability", False), now=now)
        reconcile_workstream_case(db, incomplete, now=now)
        db.commit()
        case = db.query(AssuranceCase).filter(
            AssuranceCase.case_type == "monday_workstream").one()
        assert case.status == "open"

        complete = evaluate_workstream(
            "deployment_reliability", _evidence("deployment_reliability", True),
            now=now + timedelta(minutes=5))
        reconcile_workstream_case(db, complete, now=now + timedelta(minutes=5))
        db.commit()
        assert case.status == "resolved"
        assert case.resolved_at is not None

        reconcile_workstream_case(db, incomplete, now=now + timedelta(minutes=10))
        db.commit()
        assert case.status == "open"
        assert case.resolved_at is None


def test_accountability_collection_persists_one_case_per_workstream():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = MONDAY_DEADLINE - timedelta(days=1)
    with Session(engine) as db:
        results = workstream_statuses(db, now=now, persist_cases=True)
        db.commit()
        assert len(results) == 4
        assert all(result["status"] == "at_risk" for result in results)
        assert db.query(AssuranceCase).filter(
            AssuranceCase.case_type == "monday_workstream").count() == 4
