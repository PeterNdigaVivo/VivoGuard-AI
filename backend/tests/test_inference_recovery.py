import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.utils.inference_recovery import inference_recovered


NOW = 1_000.0
HEALTH = {
    "last_run_ts": 980,
    "cameras_total": 10,
    "cameras_fresh": 8,
    "cameras_actively_inferencing": 2,
    "critical_cameras_overdue": 0,
}


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({}, True),
        ({"cameras_total": 0, "cameras_fresh": 0, "cameras_actively_inferencing": 0}, True),
        ({"cameras_total": 0}, False),
        ({"cameras_fresh": 0, "cameras_actively_inferencing": 0}, False),
        ({"cameras_actively_inferencing": 0}, False),
        ({"cameras_fresh": None}, False),
        ({"critical_cameras_overdue": None}, False),
        ({"critical_cameras_overdue": 1}, False),
        ({"last_run_ts": NOW - 120}, True),
        ({"last_run_ts": NOW - 121}, False),
        ({"last_run_ts": NOW + 1}, False),
    ],
)
def test_inference_recovery_requires_current_coverage(metrics, expected):
    assert inference_recovered({**HEALTH, **metrics}, now=NOW) is expected


@pytest.mark.parametrize("fresh", [8, 0])
def test_recovery_command_exit_code(fresh):
    heartbeat = {**HEALTH, "last_run_ts": time.time(), "cameras_fresh": fresh}
    command = [sys.executable, "-B", "-m", "app.utils.inference_recovery"]
    result = subprocess.run(
        command, input=json.dumps(heartbeat), text=True, capture_output=True,
        cwd=Path(__file__).resolve().parents[1], timeout=10, check=False,
    )
    assert result.returncode == (0 if fresh else 1)


def test_recovery_summary_prints_only_known_aggregate_fields():
    heartbeat = {
        **HEALTH, "last_run_ts": "invalid-timestamp", "cameras_fresh": "opaque-value",
        "inference_queue_depth": 3, "fresh_camera_ids": [199], "details": "private detail",
    }
    command = [sys.executable, "-B", "-m", "app.utils.inference_recovery", "--summary"]
    result = subprocess.run(
        command, input=json.dumps(heartbeat), text=True, capture_output=True,
        cwd=Path(__file__).resolve().parents[1], timeout=10, check=True,
    )

    assert json.loads(result.stdout.removeprefix("latest inference health: ")) == {
        "heartbeat_age_seconds": None,
        "cameras_total": 10,
        "cameras_fresh": None,
        "cameras_actively_inferencing": 2,
        "critical_cameras_overdue": 0,
        "inference_queue_depth": 3,
    }
