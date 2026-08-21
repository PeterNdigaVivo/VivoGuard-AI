"""Fast deterministic scenarios for failure modes that may be rare in production."""

SCENARIOS = [
    {"id": "static-chair-no-alert", "kind": "person_motion", "inputs": {"moving": False, "motion_history": False}, "expected": {"alert": False}},
    {"id": "person-walks-then-pauses", "kind": "person_motion", "inputs": {"moving": False, "motion_history": True}, "expected": {"alert": True}},
    {"id": "critical-zone-camera-loss", "kind": "coverage", "inputs": {"required": 1, "fresh": 0, "clip": False}, "expected": {"status": "critical"}},
    {"id": "critical-zone-no-clip", "kind": "coverage", "inputs": {"required": 1, "fresh": 1, "clip": False}, "expected": {"status": "warning"}},
    {"id": "critical-alert-unacknowledged", "kind": "alert_sla", "inputs": {"critical": True, "age_seconds": 360, "acknowledged": False}, "expected": {"breach": True}},
    {"id": "normal-alert-within-sla", "kind": "alert_sla", "inputs": {"critical": False, "age_seconds": 600, "acknowledged": False}, "expected": {"breach": False}},
    {"id": "single-person-after-close", "kind": "lone_worker", "inputs": {"after_hours": True, "people": 1}, "expected": {"review": True}},
    {"id": "two-people-after-close", "kind": "lone_worker", "inputs": {"after_hours": True, "people": 2}, "expected": {"review": False}},
    {"id": "pos-no-sale-after-hours", "kind": "risk", "inputs": {"event_type": "no_sale", "amount": 75000, "after_hours": True, "camera_evidence": False}, "expected": {"band": "high_review", "human_review_required": True, "accusation": False}},
    {"id": "delivery-approved-window", "kind": "delivery", "inputs": {"within_window": True, "camera_evidence": True}, "expected": {"review": False}},
    {"id": "delivery-outside-window", "kind": "delivery", "inputs": {"within_window": False, "camera_evidence": True}, "expected": {"review": True}},
    {"id": "stale-frame", "kind": "frame_health", "inputs": {"age_seconds": 900, "max_age_seconds": 120}, "expected": {"issue": "stale_frame"}},
    {"id": "duplicate-alert-flood", "kind": "dedup", "inputs": {"same_fingerprint_count": 20}, "expected": {"emitted": 1}},
    {"id": "ambiguous-whatsapp-yes", "kind": "feedback", "inputs": {"store": None, "camera": None, "occurred_at": None, "observed": "yes", "expected": None}, "expected": {"clarification_required": True, "training_eligible": False}},
]
