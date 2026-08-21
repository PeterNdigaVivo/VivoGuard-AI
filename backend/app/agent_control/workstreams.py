"""Temporary, evidence-backed workstreams for the Monday readiness push.

These are owned delivery workstreams, not autonomous agents.  The existing
accountability agent evaluates them every five minutes and reconciles one
assurance case per workstream until the deadline has passed or every objective
completion check is satisfied.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agent_control.policies import AGENT_POLICIES
from app.models import (
    AgentReport, Alert, AssuranceCase, Camera, DetectionEvent, RiskReview,
    TrainingImage,
)

MONDAY_DEADLINE = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)  # 08:00 EAT
WATCHDOG_INTERVAL_SECONDS = 600

MONDAY_WORKSTREAMS = {
    "deployment_reliability": {
        "owner": "Platform Engineering",
        "deliverables": [
            "Keep API, database, Redis, workers and watchdog healthy through store opening.",
            "Restore frontend error-log observability and remove hidden probe failures.",
            "Produce fresh five-minute accountability evidence after the production release.",
        ],
        "sla": {
            "review_cadence_seconds": 300,
            "critical_acknowledgement_seconds": 300,
            "recovery_verification_seconds": 900,
            "deadline_at": MONDAY_DEADLINE.isoformat(),
        },
        "evidence_sources": [
            "agent_reports:backend_health,frontend,db_admin,watchdog",
            "Redis inference supervisor heartbeat reported by backend_health",
            "watchdog dead/suspended-agent evidence",
        ],
        "completion_criteria": [
            "required_platform_agents_fresh",
            "backend_dependencies_healthy",
            "frontend_observability_healthy",
            "watchdog_has_no_dead_or_suspended_agents",
        ],
    },
    "cctv_engineering": {
        "owner": "CCTV Engineering",
        "deliverables": [
            "Restore every AI-enabled production camera to fresh online telemetry.",
            "Configure and validate critical-zone coverage and retrievable incident evidence.",
            "Keep detector and stream-health monitoring fresh without hidden probe errors.",
        ],
        "sla": {
            "review_cadence_seconds": 300,
            "critical_acknowledgement_seconds": 300,
            "camera_recovery_seconds": 1800,
            "deadline_at": MONDAY_DEADLINE.isoformat(),
        },
        "evidence_sources": [
            "cameras.status,last_seen_at,ai_enabled,is_deleted",
            "agent_reports:streamer,coverage_assurance,detector_alerts",
            "critical_zone_requirements and recording-clip coverage assessments",
        ],
        "completion_criteria": [
            "all_ai_cameras_online_and_fresh",
            "streamer_reports_zero_dark_cameras",
            "all_critical_zone_requirements_pass",
            "detector_monitoring_fresh_and_error_free",
        ],
    },
    "simulation_evaluation": {
        "owner": "ML Quality",
        "deliverables": [
            "Run the isolated rare-event scenario catalog at least hourly.",
            "Achieve a 100% deterministic pass rate with no production side effects.",
            "Keep synthetic results quarantined from alerts and training eligibility.",
        ],
        "sla": {
            "review_cadence_seconds": 300,
            "scenario_run_seconds": 3600,
            "failed_scenario_acknowledgement_seconds": 300,
            "deadline_at": MONDAY_DEADLINE.isoformat(),
        },
        "evidence_sources": [
            "agent_reports:scenario_simulator",
            "isolated simulation catalog result and pass rate",
            "production_alerts_created and training_eligible side-effect guards",
        ],
        "completion_criteria": [
            "scenario_evidence_fresh",
            "all_catalog_scenarios_pass",
            "simulation_has_no_production_side_effects",
            "synthetic_evidence_not_training_eligible",
        ],
    },
    "human_validation": {
        "owner": "Loss Prevention Operations",
        "deliverables": [
            "Clarify ambiguous WhatsApp feedback within its severity deadline and follow up once.",
            "Acknowledge consequential safety alerts and complete pending human risk reviews.",
            "Prevent ambiguous, synthetic or unverified evidence from entering training.",
        ],
        "sla": {
            "review_cadence_seconds": 300,
            "urgent_feedback_seconds": 900,
            "high_feedback_seconds": 1800,
            "critical_alert_acknowledgement_seconds": 300,
            "risk_review_seconds": 1800,
            "deadline_at": MONDAY_DEADLINE.isoformat(),
        },
        "evidence_sources": [
            "agent_reports:alert_quality,lone_worker",
            "assurance_cases:feedback_clarification,human_feedback,missed_event",
            "alerts joined to detection_events for consequential classes",
            "risk_reviews human-review status",
            "training_images provenance, eligibility and review_state",
        ],
        "completion_criteria": [
            "human_validation_monitoring_fresh",
            "no_overdue_feedback_clarifications",
            "no_unacknowledged_critical_alerts",
            "no_overdue_human_risk_reviews",
            "no_unsafe_training_eligibility",
        ],
    },
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _latest_report(db: Session, name: str, now: datetime) -> dict:
    report = (db.query(AgentReport).filter(AgentReport.agent_name == name)
              .order_by(AgentReport.run_at.desc()).first())
    policy = AGENT_POLICIES.get(name)
    fresh_limit = (int(policy["interval_seconds"]) + int(policy["start_grace_seconds"])
                   if policy else WATCHDOG_INTERVAL_SECONDS + 60)
    age = None if not report or not report.run_at else (now - _aware(report.run_at)).total_seconds()
    findings = report.findings if report and isinstance(report.findings, dict) else {}
    return {
        "present": report is not None,
        "fresh": age is not None and age <= fresh_limit,
        "age_seconds": age,
        "status": report.status if report else None,
        "error_message": report.error_message if report else None,
        "probe_errors": findings.get("_errors") or {},
        "findings": findings,
        "gaps": report.gaps if report else None,
    }


def evaluate_workstream(name: str, evidence: dict, *, now: datetime) -> dict:
    """Evaluate named boolean checks against the published objective criteria."""
    definition = MONDAY_WORKSTREAMS[name]
    checks = evidence["checks"]
    expected = definition["completion_criteria"]
    missing = [criterion for criterion in expected if checks.get(criterion) is not True]
    complete = not missing
    deadline_breached = not complete and now > MONDAY_DEADLINE
    status = "complete" if complete else ("overdue" if deadline_breached else "at_risk")
    return {
        "workstream": name,
        "temporary": True,
        "owner": definition["owner"],
        "status": status,
        "complete": complete,
        "completion_percentage": round(100 * (len(expected) - len(missing)) / len(expected), 1),
        "breaches": missing,
        "deadline_breached": deadline_breached,
        "deadline_at": MONDAY_DEADLINE.isoformat(),
        "deliverables": definition["deliverables"],
        "sla": definition["sla"],
        "evidence_sources": definition["evidence_sources"],
        "completion_criteria": expected,
        "evidence": evidence,
        "evaluated_at": now.isoformat(),
    }


def _deployment_evidence(db: Session, now: datetime) -> dict:
    reports = {name: _latest_report(db, name, now)
               for name in ("backend_health", "frontend", "db_admin", "watchdog")}
    backend = reports["backend_health"]
    frontend = reports["frontend"]
    watchdog = reports["watchdog"]
    watchdog_findings = watchdog["findings"]
    checks = {
        "required_platform_agents_fresh": all(r["fresh"] for r in reports.values()),
        "backend_dependencies_healthy": (
            backend["status"] == "ok" and not backend["probe_errors"]
            and backend["findings"].get("db") == "ok"
            and backend["findings"].get("redis") == "ok"
        ),
        "frontend_observability_healthy": (
            frontend["status"] == "ok" and not frontend["probe_errors"]
            and not frontend["gaps"]
        ),
        "watchdog_has_no_dead_or_suspended_agents": (
            watchdog["fresh"] and not watchdog_findings.get("dead")
            and not watchdog_findings.get("suspended")
        ),
    }
    return {"checks": checks, "agent_reports": reports}


def _cctv_evidence(db: Session, now: datetime) -> dict:
    reports = {name: _latest_report(db, name, now)
               for name in ("streamer", "coverage_assurance", "detector_alerts")}
    stale_cutoff = now - timedelta(seconds=120)
    active_cameras = (db.query(Camera).filter(
        Camera.ai_enabled.is_(True), Camera.is_deleted.is_(False)).all())
    unavailable_ids = [camera.id for camera in active_cameras if (
        camera.status != "online" or not camera.last_seen_at
        or _aware(camera.last_seen_at) < stale_cutoff)]
    streamer = reports["streamer"]
    coverage = reports["coverage_assurance"]
    detector = reports["detector_alerts"]
    checks = {
        "all_ai_cameras_online_and_fresh": not unavailable_ids,
        "streamer_reports_zero_dark_cameras": (
            streamer["fresh"] and streamer["status"] == "ok"
            and streamer["findings"].get("dark") == 0
        ),
        "all_critical_zone_requirements_pass": (
            coverage["fresh"] and coverage["findings"].get("requirements", 0) > 0
            and coverage["findings"].get("failures") == 0
        ),
        "detector_monitoring_fresh_and_error_free": (
            detector["fresh"] and not detector["error_message"]
            and not detector["probe_errors"] and detector["status"] != "critical"
        ),
    }
    return {"checks": checks, "agent_reports": reports,
            "active_ai_cameras": len(active_cameras),
            "unavailable_camera_count": len(unavailable_ids),
            "unavailable_camera_ids": unavailable_ids[:100]}


def _simulation_evidence(db: Session, now: datetime) -> dict:
    report = _latest_report(db, "scenario_simulator", now)
    findings = report["findings"]
    checks = {
        "scenario_evidence_fresh": report["fresh"],
        "all_catalog_scenarios_pass": (
            findings.get("total", 0) > 0 and findings.get("failed") == 0
            and findings.get("pass_rate") == 1.0
        ),
        "simulation_has_no_production_side_effects": (
            findings.get("execution_mode") == "isolated_simulation"
            and findings.get("production_alerts_created") == 0
        ),
        "synthetic_evidence_not_training_eligible": (
            findings.get("synthetic") is True
            and findings.get("training_eligible") is False
        ),
    }
    return {"checks": checks, "scenario_report": report}


def _human_evidence(db: Session, now: datetime) -> dict:
    monitoring_reports = {name: _latest_report(db, name, now)
                          for name in ("alert_quality", "lone_worker")}
    clarification_cases = (db.query(AssuranceCase).filter(
        AssuranceCase.case_type == "feedback_clarification",
        AssuranceCase.status != "resolved").limit(500).all())
    overdue_clarifications = []
    for case in clarification_cases:
        raw_due = (case.evidence or {}).get("clarification_due_at")
        try:
            due = _aware(datetime.fromisoformat(raw_due)) if raw_due else None
        except (TypeError, ValueError):
            due = None
        if due is None or due < now:
            overdue_clarifications.append(case.id)

    critical_types = {"weapon", "brandished_weapon", "fight", "fire", "smoke", "intrusion"}
    critical_cutoff = now - timedelta(minutes=5)
    unacknowledged_critical = (db.query(Alert.id).join(
        DetectionEvent, DetectionEvent.id == Alert.event_id).filter(
            Alert.status.in_(("new", "escalated")),
            Alert.acknowledged_at.is_(None),
            Alert.created_at < critical_cutoff,
            DetectionEvent.detection_type.in_(critical_types),
        ).limit(500).all())
    risk_cutoff = now - timedelta(minutes=30)
    overdue_reviews = (db.query(RiskReview.id).filter(
        RiskReview.status == "pending_human_review",
        RiskReview.created_at < risk_cutoff).limit(500).all())
    unsafe_training = (db.query(TrainingImage.id).filter(
        TrainingImage.eligible_for_training.is_(True),
        (
            TrainingImage.source_kind.in_(("synthetic", "simulation"))
            | ((TrainingImage.source_kind == "human_missed_event")
               & (TrainingImage.review_state != "approved"))
        ),
    ).limit(500).all())
    checks = {
        "human_validation_monitoring_fresh": all(
            report["fresh"] and not report["error_message"]
            and not report["probe_errors"] for report in monitoring_reports.values()),
        "no_overdue_feedback_clarifications": not overdue_clarifications,
        "no_unacknowledged_critical_alerts": not unacknowledged_critical,
        "no_overdue_human_risk_reviews": not overdue_reviews,
        "no_unsafe_training_eligibility": not unsafe_training,
    }
    return {
        "checks": checks,
        "agent_reports": monitoring_reports,
        "open_clarification_count": len(clarification_cases),
        "overdue_clarification_ids": overdue_clarifications,
        "unacknowledged_critical_alert_ids": [row[0] for row in unacknowledged_critical],
        "overdue_risk_review_ids": [row[0] for row in overdue_reviews],
        "unsafe_training_image_ids": [row[0] for row in unsafe_training],
    }


def reconcile_workstream_case(db: Session, result: dict, *, now: datetime) -> None:
    name = result["workstream"]
    key = f"monday-workstream:2026-08-24:{name}"
    case = db.query(AssuranceCase).filter(AssuranceCase.dedup_key == key).one_or_none()
    if result["complete"]:
        if case and case.status != "resolved":
            case.status = "resolved"
            case.resolved_at = now
            case.last_seen_at = now
            case.evidence = result
            case.resolution = "Objective completion criteria verified by the accountability agent."
        return
    severity = "critical" if result["deadline_breached"] else "high"
    if case:
        case.status = "open"
        case.severity = severity
        case.last_seen_at = now
        case.resolved_at = None
        case.resolution = None
        case.evidence = result
    else:
        db.add(AssuranceCase(
            dedup_key=key, case_type="monday_workstream", severity=severity,
            status="open", title=f"Monday readiness: {name.replace('_', ' ')}",
            description=(f"Accountable owner: {result['owner']}. Complete the objective "
                         "criteria by 08:00 EAT on Monday 24 August 2026."),
            evidence=result, human_review_required=True,
        ))


def workstream_statuses(db: Session, *, now: datetime | None = None,
                        persist_cases: bool = False) -> list[dict]:
    now = _aware(now or datetime.now(timezone.utc))
    collectors = {
        "deployment_reliability": _deployment_evidence,
        "cctv_engineering": _cctv_evidence,
        "simulation_evaluation": _simulation_evidence,
        "human_validation": _human_evidence,
    }
    results = [evaluate_workstream(name, collectors[name](db, now), now=now)
               for name in MONDAY_WORKSTREAMS]
    if persist_cases:
        for result in results:
            reconcile_workstream_case(db, result, now=now)
    return results
