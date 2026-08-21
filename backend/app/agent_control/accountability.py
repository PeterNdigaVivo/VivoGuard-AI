"""Evidence-backed agent SLA scorecards using existing run reports."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agent_control.policies import AGENT_POLICIES
from app.models import AgentReport, AssuranceCase


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def build_scorecard(name: str, reports: list[AgentReport], *, now: datetime,
                    window_seconds: int) -> dict:
    policy = AGENT_POLICIES[name]
    interval = int(policy["interval_seconds"])
    expected = max(1, math.floor(window_seconds / interval))
    completed = len(reports)
    successful = sum(1 for report in reports if not report.error_message)
    valid = sum(1 for report in reports if report.status in {"ok", "warning", "critical"}
                and isinstance(report.findings, dict) and bool(report.findings))
    last = max((_aware(r.run_at) for r in reports if r.run_at), default=None)
    freshness_limit = interval + int(policy["start_grace_seconds"])
    fresh = bool(last and (now - last).total_seconds() <= freshness_limit)
    availability = min(1.0, completed / expected)
    reliability = successful / completed if completed else 0.0
    output_quality = valid / completed if completed else 0.0
    score = round(100 * (0.35 * availability + 0.30 * reliability +
                         0.20 * output_quality + 0.15 * float(fresh)), 2)
    breaches = []
    if availability < float(policy["availability_target"]):
        breaches.append("run_coverage_below_99pct")
    if reliability < 0.99:
        breaches.append("completion_reliability_below_99pct")
    if output_quality < float(policy["valid_output_target"]):
        breaches.append("valid_output_below_99pct")
    if not fresh:
        breaches.append("latest_evidence_stale")
    active_critical = any(r.status == "critical" for r in reports[:1])
    if active_critical:
        breaches.append("latest_run_critical")
    return {"agent_name": name, "owner": policy["owner"], "score": score,
            "compliant": not breaches, "active_critical_override": active_critical,
            "breaches": breaches, "window_seconds": window_seconds,
            "expected_runs": expected, "completed_runs": completed,
            "successful_runs": successful, "valid_outputs": valid,
            "run_coverage": round(availability, 4), "completion_reliability": round(reliability, 4),
            "valid_output_rate": round(output_quality, 4),
            "last_report_at": last.isoformat() if last else None,
            "measurement_limitations": [
                "schedule start latency is not measurable until durable due/start timestamps are deployed",
                "notification delivery is not counted because the current WhatsApp provider is disabled"],
            "targets": {"run_coverage": 0.99, "completion_reliability": 0.99,
                        "valid_output": 0.99}}


def scorecards(db: Session, *, now: datetime | None = None, window_hours: int = 24,
               persist_cases: bool = False) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    cards = []
    for name, policy in AGENT_POLICIES.items():
        reports = (db.query(AgentReport).filter(AgentReport.agent_name == name,
                                                AgentReport.run_at >= since)
                   .order_by(AgentReport.run_at.desc()).all())
        card = build_scorecard(name, reports, now=now, window_seconds=window_hours * 3600)
        cards.append(card)
        if persist_cases:
            key = f"agent-sla:{name}"
            case = db.query(AssuranceCase).filter(AssuranceCase.dedup_key == key).one_or_none()
            if card["compliant"]:
                if case and case.status != "resolved":
                    case.status, case.resolved_at = "resolved", now
                    case.resolution = "Agent returned to SLA; recovery verified from run reports."
            elif case:
                case.status, case.last_seen_at, case.evidence = "open", now, card
            else:
                db.add(AssuranceCase(dedup_key=key, case_type="agent_sla", severity="critical",
                                     status="open", title=f"Agent SLA breach: {name}",
                                     description=f"Owner: {policy['owner']}. Restore cadence and evidence quality.",
                                     evidence=card, human_review_required=True))
    return cards
