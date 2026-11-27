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


def read_release_sample(
    broker, *, queue_names: list[str], after: int, min_fresh: int,
    min_recording: int, max_queue: int, now: float | None = None,
) -> tuple[float, int, int, int, int, int] | None:
    """Check current broker depth alongside fresh, authoritative coverage."""
    from app.tasks.inference import REDIS_PRIORITY_SEP

    if not queue_names:
        return None
    # Read all priority lists and both heartbeats in one Redis transaction.
    # The heartbeat's queue count was taken during batch dispatch and can
    # outlive the work it measured. Retain it beside the live count for logs.
    with broker.pipeline(transaction=True) as pipe:
        pipe.mget("vg:recording:health", "vg:inference:health")
        for queue in queue_names:
            pipe.llen(queue)
            for priority in range(1, 10):
                pipe.llen(f"{queue}{REDIS_PRIORITY_SEP}{priority}")
        heartbeats, *depths = pipe.execute()

    observed_at = time.time() if now is None else now
    try:
        recording, inference = (json.loads(raw) for raw in heartbeats)
    except (TypeError, ValueError):
        return None
    if not inference_recovered(inference, now=observed_at) or not isinstance(recording, dict):
        return None
    inference_age = _heartbeat_age(inference, observed_at)
    recording_age = _heartbeat_age(recording, observed_at)
    if inference_age is None or not 0 <= inference_age < 90:
        return None
    if recording_age is None or not 0 <= recording_age < 120:
        return None
    stamp = float(inference["last_run_ts"])
    if min(stamp, float(recording["last_run_ts"])) <= after:
        return None

    fresh = inference["cameras_fresh"]
    active = inference["cameras_actively_inferencing"]
    recording_count = recording.get("cameras_recording")
    expected_recording = recording.get("cameras_expected")
    standard_overdue = inference.get("standard_cameras_overdue")
    dispatch_depth = inference.get("inference_queue_depth")
    counts = (recording_count, expected_recording, standard_overdue, dispatch_depth, *depths)
    if len(depths) != 10 * len(queue_names) or any(
        type(count) is not int or count < 0 for count in counts
    ):
        return None
    live_depth = sum(depths)
    if (
        fresh < min_fresh or not 0 < active <= fresh
        or not min_recording <= recording_count <= expected_recording
        or standard_overdue != 0 or live_depth > max_queue
    ):
        return None
    return stamp, fresh, recording_count, active, live_depth, dispatch_depth


def _read_configured_release_sample(
    limits: list[int],
) -> tuple[float, int, int, int, int, int] | None:
    import redis

    from app.config import settings
    from app.tasks.inference import _inference_queue_names

    after, min_fresh, min_recording, max_queue = limits
    if min(limits) < 0:
        return None
    try:
        with redis.Redis.from_url(
            settings.redis_url, socket_timeout=3, socket_connect_timeout=3,
        ) as broker:
            return read_release_sample(
                broker, queue_names=_inference_queue_names(), after=after,
                min_fresh=min_fresh, min_recording=min_recording, max_queue=max_queue,
            )
    except redis.RedisError:
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--summary", action="store_true")
    mode.add_argument(
        "--release-sample", nargs=4, type=int,
        metavar=("AFTER", "MIN_FRESH", "MIN_RECORDING", "MAX_QUEUE"),
    )
    args = parser.parse_args()
    if args.release_sample is not None:
        sample = _read_configured_release_sample(args.release_sample)
        if sample is None:
            sys.exit(1)
        print(*sample)
        sys.exit(0)
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
