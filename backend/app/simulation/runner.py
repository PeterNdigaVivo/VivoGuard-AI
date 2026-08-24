"""Pure scenario evaluator with a hard production side-effect boundary."""
from __future__ import annotations

from collections import defaultdict

from app.risk.scoring import risk_band, score_operational_event
from app.simulation.catalog import SCENARIOS

EXPLICIT_FEEDBACK_LABELS = {
    "true_alert", "false_alert", "missed_event", "operational_issue",
}


def missing_feedback_fields(inputs: dict) -> list[str]:
    missing = []
    ambiguous = {"yes", "no", "ok", "fine", "looks fine", "same", "this", "that"}
    for field in ("store", "camera", "occurred_at", "observed", "expected"):
        value = inputs.get(field)
        if value is None or (isinstance(value, str) and
                             (len(value.strip()) < 3 or value.strip().lower() in ambiguous)):
            missing.append(field)
    if str(inputs.get("label") or "").strip().lower() not in EXPLICIT_FEEDBACK_LABELS:
        missing.append("label")
    return missing


def evaluate(kind: str, inputs: dict) -> dict:
    if kind == "intrusion_incident":
        actionable_person = bool(inputs.get("motion_history"))
        fingerprint_active = bool(inputs.get("incident_fingerprint_active"))
        alert = actionable_person and not fingerprint_active
        return {"alert": alert,
                "duplicate_suppressed": fingerprint_active}
    if kind == "crowd_policy":
        staff_priority = bool(inputs.get("staff_zone_overlap"))
        alert = (int(inputs.get("people", 0)) >= 6
                 and float(inputs.get("stationary_seconds", 0)) >= 300
                 and not staff_priority)
        return {"alert": alert, "staff_area_priority": staff_priority}
    if kind == "quality_policy":
        detected = bool(inputs.get("detected"))
        review_only = str(inputs.get("mode")) in {"review_only", "quarantined"}
        return {"alert_recorded": detected, "evidence_retained": detected,
                "notification_sent": detected and not review_only,
                "training_eligible": detected and not review_only,
                "review_only": review_only}
    if kind == "person_motion":
        return {"alert": bool(inputs.get("moving") or inputs.get("motion_history"))}
    if kind == "track_rearm":
        same_track = bool(inputs.get("same_track"))
        elapsed = max(0.0, float(inputs.get("elapsed_seconds", 0)))
        rearm = max(0.0, float(inputs.get("rearm_seconds", 0)))
        suppressed = same_track and elapsed < rearm
        return {"alert": not suppressed,
                "duplicate_suppressed": suppressed,
                "rearmed": not suppressed}
    if kind == "protected_zone":
        excluded = bool(inputs.get("inside_excluded_public_area"))
        dwell_satisfied = float(inputs.get("dwell_seconds", 0)) >= float(
            inputs.get("minimum_dwell_seconds", 0))
        alert = (bool(inputs.get("inside_protected_zone"))
                 and not excluded and dwell_satisfied)
        return {"alert": alert, "excluded_as_public": excluded,
                "dwell_satisfied": dwell_satisfied}
    if kind == "camera_handoff":
        source_id, target_id = inputs.get("source_global_id"), inputs.get("target_global_id")
        in_time = float(inputs.get("gap_seconds", 0)) <= float(inputs.get("max_gap_seconds", 15))
        preserved = bool(source_id and target_id and source_id == target_id and in_time)
        expected_same = bool(inputs.get("expected_same_person"))
        return {"identity_preserved": preserved, "id_loss": not bool(target_id),
                "review": expected_same and not preserved,
                "unique_count_confident": preserved or not expected_same}
    if kind == "coverage":
        if int(inputs.get("fresh", 0)) < int(inputs.get("required", 1)):
            return {"status": "critical"}
        return {"status": "pass" if inputs.get("clip") else "warning"}
    if kind == "clip_sla":
        eligible = max(0, int(inputs.get("eligible", 0)))
        available = max(0, int(inputs.get("available", 0)))
        rate = _rate(min(available, eligible), eligible)
        valid = eligible > 0
        return {"availability_rate": rate,
                "breach": bool(valid and rate is not None and rate < float(
                    inputs.get("minimum_rate", 0.95))),
                "denominator_valid": valid}
    if kind == "merchandise_flow":
        outbound = max(0, int(inputs.get("outbound_qty", 0)))
        matched = sum(max(0, int(inputs.get(key, 0)))
                      for key in ("sold_qty", "transfer_qty", "return_qty"))
        unmatched = max(0, outbound - matched)
        evidence = bool(inputs.get("camera_evidence"))
        reason = ("camera_evidence_unavailable" if not evidence
                  else "unmatched_merchandise_flow" if unmatched else None)
        return {"unmatched_qty": unmatched, "review": bool(unmatched or not evidence),
                "reason": reason, "accusation": False}
    if kind == "zone_boundary":
        spatial = float(inputs.get("inside_ratio", 0)) >= float(
            inputs.get("min_inside_ratio", 0.6))
        temporal = int(inputs.get("consecutive_frames", 0)) >= int(
            inputs.get("min_frames", 3))
        alert = spatial and temporal
        return {"alert": alert, "suppressed_as_boundary": not alert}
    if kind == "alert_sla":
        sla = 300 if inputs.get("critical") else 1800
        return {"breach": (not inputs.get("acknowledged")
                           and inputs.get("age_seconds", 0) > sla)}
    if kind == "alert_pipeline":
        occurrences = max(0, int(inputs.get("occurrences", 0)))
        fingerprints = max(0, int(inputs.get("unique_fingerprints", 0)))
        emitted = min(occurrences, fingerprints)
        duplicate_suppressed = max(0, occurrences - emitted)
        latency_breach = float(inputs.get("latency_seconds", 0)) > float(
            inputs.get("max_latency_seconds", 120))
        ack_sla = 300 if inputs.get("critical") else 1800
        ack_breach = (not inputs.get("acknowledged")
                      and float(inputs.get("age_seconds", 0)) > ack_sla)
        return {"latency_breach": latency_breach,
                "acknowledgement_breach": ack_breach,
                "duplicate_suppressed": duplicate_suppressed,
                "emitted": emitted, "breach": latency_breach or ack_breach}
    if kind == "lone_worker":
        review = bool(inputs.get("after_hours") and inputs.get("people") == 1)
        confidence = ("supported" if inputs.get("tracking_reliable")
                      else "needs_confirmation")
        return {"review": review, "confidence": confidence}
    if kind == "event_correlation":
        source = bool(inputs.get("source_event"))
        camera = bool(inputs.get("camera_evidence"))
        delta = inputs.get("time_delta_seconds")
        correlated = bool(source and camera and delta is not None
                          and abs(float(delta)) <= float(
                              inputs.get("max_delta_seconds", 300)))
        reason = None
        if not source:
            reason = "source_event_unavailable"
        elif not camera:
            reason = "camera_evidence_unavailable"
        elif not correlated:
            reason = "outside_correlation_window"
        return {"correlated": correlated, "review": not correlated,
                "reason": reason, "accusation": False}
    if kind == "risk":
        score, factors = score_operational_event(
            inputs["event_type"], inputs.get("amount"),
            after_hours=bool(inputs.get("after_hours")),
            camera_evidence=bool(inputs.get("camera_evidence")))
        return {"score": score, "band": risk_band(score), "factors": factors,
                "human_review_required": True, "accusation": False}
    if kind == "delivery":
        return {"review": (not bool(inputs.get("within_window"))
                           or not bool(inputs.get("camera_evidence")))}
    if kind == "frame_health":
        if not inputs.get("online") or not inputs.get("has_frame"):
            issue = "offline_feed"
        elif inputs.get("age_seconds", 0) > inputs.get("max_age_seconds", 120):
            issue = "stale_frame"
        else:
            issue = None
        return {"issue": issue, "review": issue is not None}
    if kind == "feedback":
        missing = missing_feedback_fields(inputs)
        # Even explicit synthetic feedback remains evaluation-only. label_ready
        # means an equivalent REAL report could proceed to human review.
        return {"clarification_required": bool(missing), "missing_fields": missing,
                "label_ready": not missing, "training_eligible": False}
    raise ValueError(f"unknown simulation kind: {kind}")


def _binary_action(scenario: dict, payload: dict) -> bool | None:
    key = scenario.get("action_key")
    return bool(payload.get(key)) if key else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def run_catalog() -> dict:
    results = []
    by_domain: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0, "failed": 0})
    by_severity: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0, "failed": 0})
    tp = fp = tn = fn = 0
    for scenario in SCENARIOS:
        observed = evaluate(scenario["kind"], scenario["inputs"])
        failures = {key: {"expected": expected, "observed": observed.get(key)}
                    for key, expected in scenario["expected"].items()
                    if observed.get(key) != expected}
        passed = not failures
        expected_action = _binary_action(scenario, scenario["expected"])
        observed_action = _binary_action(scenario, observed)
        if expected_action is not None:
            if expected_action and observed_action:
                tp += 1
            elif expected_action and not observed_action:
                fn += 1
            elif not expected_action and observed_action:
                fp += 1
            else:
                tn += 1
        domain, severity = scenario["domain"], scenario["severity"]
        for bucket in (by_domain[domain], by_severity[severity]):
            bucket["total"] += 1
            bucket["passed" if passed else "failed"] += 1
        results.append({"scenario_id": scenario["id"], "domain": domain,
                        "severity": severity, "passed": passed,
                        "expected_action": expected_action,
                        "observed_action": observed_action,
                        "failures": failures, "observed": observed})
    passed_count = sum(1 for result in results if result["passed"])
    failed_ids = [result["scenario_id"] for result in results if not result["passed"]]
    isolation = {"production_alerts_created": 0, "notifications_sent": 0,
                 "training_samples_created": 0, "training_eligible": False}
    blockers = ([f"scenario_failed:{scenario_id}" for scenario_id in failed_ids]
                + [key for key, value in isolation.items()
                   if key != "training_eligible" and value != 0])
    return {
        "execution_mode": "isolated_simulation", "synthetic": True,
        **isolation, "total": len(results), "passed": passed_count,
        "failed": len(results) - passed_count,
        "pass_rate": _rate(passed_count, len(results)),
        "action_metrics": {"true_positive": tp, "false_positive": fp,
                           "true_negative": tn, "false_negative": fn,
                           "precision": _rate(tp, tp + fp),
                           "recall": _rate(tp, tp + fn),
                           "note": ("Synthetic policy-action accuracy; not "
                                    "production alert precision.")},
        "by_domain": dict(sorted(by_domain.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "release_gate": {"passed": not blockers, "blockers": blockers,
                         "scope": "isolated policy scenarios only"},
        "results": results,
    }
