"""Pure scenario runner with a hard side-effect boundary."""
from __future__ import annotations

from app.risk.scoring import risk_band, score_operational_event
from app.simulation.catalog import SCENARIOS


def missing_feedback_fields(inputs: dict) -> list[str]:
    missing = []
    ambiguous = {"yes", "no", "ok", "fine", "looks fine", "same", "this", "that"}
    for field in ("store", "camera", "occurred_at", "observed", "expected"):
        value = inputs.get(field)
        if value is None or (isinstance(value, str) and
                             (len(value.strip()) < 3 or value.strip().lower() in ambiguous)):
            missing.append(field)
    return missing


def evaluate(kind: str, inputs: dict) -> dict:
    if kind == "person_motion":
        return {"alert": bool(inputs.get("moving") or inputs.get("motion_history"))}
    if kind == "coverage":
        if int(inputs.get("fresh", 0)) < int(inputs.get("required", 1)):
            return {"status": "critical"}
        return {"status": "pass" if inputs.get("clip") else "warning"}
    if kind == "alert_sla":
        sla = 300 if inputs.get("critical") else 1800
        return {"breach": not inputs.get("acknowledged") and inputs.get("age_seconds", 0) > sla}
    if kind == "lone_worker":
        return {"review": bool(inputs.get("after_hours") and inputs.get("people") == 1)}
    if kind == "risk":
        score, factors = score_operational_event(
            inputs["event_type"], inputs.get("amount"), after_hours=bool(inputs.get("after_hours")),
            camera_evidence=bool(inputs.get("camera_evidence")))
        return {"score": score, "band": risk_band(score), "factors": factors,
                "human_review_required": True, "accusation": False}
    if kind == "delivery":
        return {"review": not bool(inputs.get("within_window")) or not bool(inputs.get("camera_evidence"))}
    if kind == "frame_health":
        issue = "stale_frame" if inputs.get("age_seconds", 0) > inputs.get("max_age_seconds", 120) else None
        return {"issue": issue}
    if kind == "dedup":
        return {"emitted": 1 if inputs.get("same_fingerprint_count", 0) else 0}
    if kind == "feedback":
        missing = missing_feedback_fields(inputs)
        return {"clarification_required": bool(missing), "missing_fields": missing,
                "training_eligible": False if missing else None}
    raise ValueError(f"unknown simulation kind: {kind}")


def run_catalog() -> dict:
    results = []
    for scenario in SCENARIOS:
        observed = evaluate(scenario["kind"], scenario["inputs"])
        failures = {key: {"expected": expected, "observed": observed.get(key)}
                    for key, expected in scenario["expected"].items() if observed.get(key) != expected}
        results.append({"scenario_id": scenario["id"], "passed": not failures,
                        "failures": failures, "observed": observed})
    passed = sum(1 for result in results if result["passed"])
    return {"execution_mode": "isolated_simulation", "synthetic": True,
            "training_eligible": False, "production_alerts_created": 0,
            "total": len(results), "passed": passed, "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4), "results": results}
