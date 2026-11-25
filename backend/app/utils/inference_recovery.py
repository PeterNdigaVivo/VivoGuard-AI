"""Check the supervisor heartbeat before a deployment reports recovery."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time


def _heartbeat_age(health: dict, now: float) -> float | None:
    try:
        age = now - float(health["last_run_ts"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return age if math.isfinite(age) else None


def inference_recovered(health: object, *, now: float) -> bool:
    if not isinstance(health, dict):
        return False
    age = _heartbeat_age(health, now)
    if age is None or not 0 <= age <= 120:
        return False

    total = health.get("cameras_total")
    fresh = health.get("cameras_fresh")
    active = health.get("cameras_actively_inferencing")
    overdue = health.get("critical_cameras_overdue")
    if any(type(count) is not int or count < 0 for count in (total, fresh, active, overdue)):
        return False
    if overdue != 0:
        return False
    if total == 0:
        return fresh == 0 and active == 0
    return 0 < fresh <= total and 0 < active <= total


def recovery_summary(health: dict, *, now: float) -> dict:
    """Return only aggregate measurements suitable for deployment logs."""
    age = _heartbeat_age(health, now)
    summary = {"heartbeat_age_seconds": round(age, 1) if age is not None else None}
    for field in (
        "cameras_total", "cameras_fresh", "cameras_actively_inferencing",
        "critical_cameras_overdue", "inference_queue_depth",
    ):
        count = health.get(field)
        summary[field] = count if type(count) is int and count >= 0 else None
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    try:
        heartbeat = json.load(sys.stdin)
    except (ValueError, OSError):
        sys.exit(1)
    now = time.time()
    if args.summary:
        if not isinstance(heartbeat, dict):
            sys.exit(1)
        print("latest inference health: " + json.dumps(recovery_summary(heartbeat, now=now)))
    else:
        sys.exit(0 if inference_recovered(heartbeat, now=now) else 1)
