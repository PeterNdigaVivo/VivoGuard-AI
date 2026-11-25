"""Check the supervisor heartbeat before a deployment reports recovery."""
from __future__ import annotations

import json
import sys
import time


def inference_recovered(health: object, *, now: float) -> bool:
    if not isinstance(health, dict):
        return False
    try:
        age = now - float(health["last_run_ts"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if not 0 <= age <= 120:
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


if __name__ == "__main__":
    try:
        heartbeat = json.load(sys.stdin)
    except (ValueError, OSError):
        sys.exit(1)
    sys.exit(0 if inference_recovered(heartbeat, now=time.time()) else 1)
