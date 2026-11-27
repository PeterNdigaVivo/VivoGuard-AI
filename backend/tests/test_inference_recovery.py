import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.utils.inference_recovery import inference_recovered, read_release_sample


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


@pytest.mark.parametrize(("offset", "expected"), [(-1, 1), (0, 1), (0.5, 1), (1.25, 0)])
def test_recovery_command_requires_heartbeat_after_service_recreation(offset, expected):
    cutoff = int(time.time()) - 5
    heartbeat = {**HEALTH, "last_run_ts": cutoff + offset}
    command = [
        sys.executable, "-B", "-m", "app.utils.inference_recovery", "--after", str(cutoff),
    ]
    result = subprocess.run(
        command, input=json.dumps(heartbeat), text=True, capture_output=True,
        cwd=Path(__file__).resolve().parents[1], timeout=10, check=False,
    )

    assert result.returncode == expected, result.stderr
    assert result.stderr == ""


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


class _BrokerSample:
    def __init__(self, inference, recording, queue_depths):
        self.heartbeats = [json.dumps(recording), json.dumps(inference)]
        self.queue_depths = queue_depths
        self.keys = []

    def pipeline(self, *, transaction):
        assert transaction is True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def mget(self, *keys):
        assert keys == ("vg:recording:health", "vg:inference:health")

    def llen(self, key):
        self.keys.append(key)

    def execute(self):
        return [self.heartbeats, *(self.queue_depths.get(key, 0) for key in self.keys)]


@pytest.mark.parametrize(
    ("inf", "rec", "queue_depth", "expected"),
    [
        ({}, {}, 5, (980, 8, 6, 2, 5, 39)),
        ({}, {}, 6, None),
        ({"inference_queue_depth": 0}, {}, 8, None),
        ({"inference_queue_depth": None}, {}, 0, None),
        ({"critical_cameras_overdue": 1}, {}, 0, None),
        ({"standard_cameras_overdue": 1}, {}, 0, None),
        ({"standard_cameras_overdue": None}, {}, 0, None),
        ({"cameras_fresh": 7}, {}, 0, None),
        ({"last_run_ts": 970}, {}, 0, None),
        ({}, {"last_run_ts": 969}, 0, None),
        ({}, {"last_run_ts": NOW + 1}, 0, None),
        ({}, {"cameras_recording": 5}, 0, None),
        ({}, {"cameras_recording": 8}, 0, None),
    ],
)
def test_release_sample_checks_live_priority_queues_and_current_coverage(
    inf, rec, queue_depth, expected,
):
    broker = _BrokerSample(
        {**HEALTH, "inference_queue_depth": 39, "standard_cameras_overdue": 0, **inf},
        {"last_run_ts": 981.5, "cameras_recording": 6, "cameras_expected": 7, **rec},
        {"inference.1\x06\x169": queue_depth},
    )
    assert read_release_sample(
        broker, queue_names=["inference.0", "inference.1"], after=970,
        min_fresh=8, min_recording=6, max_queue=5, now=NOW,
    ) == expected
    assert broker.keys == [
        name if priority == 0 else f"{name}\x06\x16{priority}"
        for name in ("inference.0", "inference.1")
        for priority in range(10)
    ]
