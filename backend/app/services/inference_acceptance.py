"""Capacity acceptance gates for the GPU batch shadow canary.

This module intentionally evaluates infrastructure capacity only. Alert
precision and recall require independently reviewed ground truth and are never
inferred from throughput telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapacityThresholds:
    max_health_age_seconds: float = 120.0
    min_uptime_seconds: float = 7200.0
    min_frames_per_camera: int = 100
    max_p95_per_frame_ms: float = 400.0
    max_schedule_wait_seconds: float = 2.0


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _id_set(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()
    result = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def evaluate_capacity_acceptance(
    authoritative: dict | None,
    shadow: dict | None,
    *,
    now: float,
    baseline: dict | None = None,
    thresholds: CapacityThresholds = CapacityThresholds(),
) -> dict:
    """Return explicit, machine-readable dark-launch acceptance evidence."""
    checks: list[dict] = []

    def add(name: str, passed: bool, actual: Any, required: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "actual": actual,
            "required": required,
        })

    authoritative_ts = _number((authoritative or {}).get("last_run_ts"))
    authoritative_age = (
        max(0.0, now - authoritative_ts) if authoritative_ts is not None else None
    )
    add(
        "authoritative_health_fresh",
        authoritative_age is not None
        and authoritative_age <= thresholds.max_health_age_seconds,
        round(authoritative_age, 2) if authoritative_age is not None else None,
        f"age <= {thresholds.max_health_age_seconds:g}s",
    )

    shadow_ts = _number((shadow or {}).get("last_run_ts"))
    shadow_age = max(0.0, now - shadow_ts) if shadow_ts is not None else None
    add(
        "shadow_health_fresh",
        shadow_age is not None and shadow_age <= thresholds.max_health_age_seconds,
        round(shadow_age, 2) if shadow_age is not None else None,
        f"age <= {thresholds.max_health_age_seconds:g}s",
    )
    add(
        "shadow_is_non_authoritative",
        bool(shadow) and shadow.get("authoritative") is False,
        (shadow or {}).get("authoritative"),
        "false",
    )

    uptime = _number((shadow or {}).get("uptime_seconds"))
    add(
        "two_hour_canary",
        uptime is not None and uptime >= thresholds.min_uptime_seconds,
        uptime,
        f">= {thresholds.min_uptime_seconds:g}s",
    )

    errors = _number((shadow or {}).get("errors"))
    add("zero_shadow_errors", errors == 0, errors, "0")

    fresh_cameras = _number((authoritative or {}).get("cameras_fresh"))
    served_cameras = _number((shadow or {}).get("cameras_served"))
    fresh_ids = _id_set((authoritative or {}).get("fresh_camera_ids"))
    served_ids = _id_set((shadow or {}).get("served_camera_ids"))
    missing_ids = sorted(fresh_ids - served_ids)
    all_fresh_served = (
        fresh_cameras is not None
        and fresh_cameras > 0
        and served_cameras is not None
        and served_cameras >= fresh_cameras
        and len(fresh_ids) == int(fresh_cameras)
        and not missing_ids
    )
    add(
        "all_fresh_cameras_served",
        all_fresh_served,
        {
            "fresh": fresh_cameras,
            "served": served_cameras,
            "missing_camera_ids": missing_ids,
        },
        "every current fresh camera ID served",
    )

    frames = _number((shadow or {}).get("frames_processed"))
    required_frames = (
        int(fresh_cameras) * thresholds.min_frames_per_camera
        if fresh_cameras is not None and fresh_cameras > 0 else None
    )
    add(
        "minimum_shadow_frames",
        frames is not None
        and required_frames is not None
        and frames >= required_frames,
        frames,
        f">= {required_frames if required_frames is not None else 'unknown'}",
    )

    p95 = _number((shadow or {}).get("p95_per_frame_ms"))
    add(
        "shadow_p95_per_frame",
        p95 is not None and p95 <= thresholds.max_p95_per_frame_ms,
        p95,
        f"<= {thresholds.max_p95_per_frame_ms:g}ms",
    )

    wait = _number((shadow or {}).get("max_camera_schedule_wait_seconds"))
    add(
        "bounded_camera_schedule_wait",
        wait is not None and wait <= thresholds.max_schedule_wait_seconds,
        wait,
        f"<= {thresholds.max_schedule_wait_seconds:g}s",
    )

    baseline_frames = _number((baseline or {}).get("frames"))
    baseline_cameras = _number((baseline or {}).get("cameras_reporting"))
    add(
        "cpu_baseline_available",
        baseline_frames is not None
        and baseline_frames > 0
        and baseline_cameras is not None
        and baseline_cameras > 0,
        baseline,
        "at least one reporting camera and frame in the last 24h",
    )

    passed = bool(checks) and all(check["passed"] for check in checks)
    has_telemetry = authoritative is not None and shadow is not None
    status = "capacity_ready" if passed else ("failed" if has_telemetry else "pending")
    return {
        "status": status,
        "capacity_gate_passed": passed,
        "promotion_ready": False,
        "accuracy_gate_evaluated": False,
        "accuracy_target": "99% precision and recall per camera-detector slice",
        "accuracy_note": (
            "Capacity telemetry cannot prove alert quality. Promotion requires "
            "independently reviewed true alerts and missed-event ground truth."
        ),
        "checks": checks,
    }
