import json
from unittest.mock import Mock

import pytest

from app.api.system import (
    _current_recording_pipeline,
    _decode_inference_pipeline,
    _proof_of_life_state,
    _runtime_camera_status,
    proof_of_life,
)


def test_fresh_frames_override_pending_configuration() -> None:
    status, age = _runtime_camera_status(
        "pending",
        {"last_frame_at": 995.0, "fps": 1.2},
        now=1000.0,
    )

    assert status == "online"
    assert age == 5.0


def test_old_frame_is_reported_as_stale() -> None:
    status, age = _runtime_camera_status(
        "online",
        {"last_frame_at": 980.0, "fps": 5.0},
        now=1000.0,
    )

    assert status == "stale"
    assert age == 20.0


def test_stream_error_is_offline_even_when_camera_is_pending() -> None:
    status, age = _runtime_camera_status(
        "pending",
        {"fps": 0, "error": "RTSP OPTIONS failed: 404 Not Found"},
        now=1000.0,
    )

    assert status == "offline"
    assert age is None


def test_pending_without_streamer_evidence_remains_pending() -> None:
    assert _runtime_camera_status("pending", {}, now=1000.0) == (
        "pending", None,
    )


def test_invalid_health_values_fail_closed() -> None:
    assert _runtime_camera_status(
        "online",
        {"last_frame_at": "not-a-timestamp", "fps": "unknown"},
        now=1000.0,
    ) == ("offline", None)


def test_inference_pipeline_telemetry_decodes_only_json_objects() -> None:
    assert _decode_inference_pipeline(b'{"cameras_fresh": 65}') == {
        "cameras_fresh": 65,
    }
    assert _decode_inference_pipeline("not-json") is None
    assert _decode_inference_pipeline("[]") is None
    assert _decode_inference_pipeline(None) is None


def test_proof_of_life_distinguishes_active_degraded_and_offline() -> None:
    now = 1_000.0
    healthy = {
        "last_run_ts": 990,
        "cameras_total": 10,
        "cameras_fresh": 10,
        "cameras_waiting_for_worker": 0,
    }
    assert _proof_of_life_state(healthy, now=now) == "active"
    assert _proof_of_life_state({**healthy, "cameras_fresh": 8}, now=now) == "degraded"
    assert _proof_of_life_state({**healthy, "cameras_waiting_for_worker": 2}, now=now) == "degraded"
    assert _proof_of_life_state(
        {**healthy, "critical_cameras_overdue": 1}, now=now,
    ) == "degraded"
    assert _proof_of_life_state(
        {**healthy, "standard_cameras_overdue": 1}, now=now,
    ) == "degraded"
    assert _proof_of_life_state({**healthy, "last_run_ts": 300}, now=now) == "offline"
    assert _proof_of_life_state(None, now=now) == "offline"


def test_batch_shadow_telemetry_retains_non_authoritative_marker() -> None:
    payload = _decode_inference_pipeline(
        b'{"mode":"shadow","authoritative":false,"frames_processed":110}'
    )
    assert payload == {
        "mode": "shadow", "authoritative": False, "frames_processed": 110,
    }


@pytest.mark.parametrize("timestamp", [None, "invalid", 820, 700, 1001, "NaN", "Infinity", True, 0])
def test_recording_pipeline_omits_unavailable_or_expired_coverage(timestamp):
    raw = json.dumps({"status": "healthy", "last_run_ts": timestamp})
    assert _current_recording_pipeline(raw, now=1_000) is None


def test_recording_pipeline_retains_fresh_degraded_coverage():
    snapshot = {
        "status": "degraded", "last_run_ts": 950,
        "cameras_recording": 43, "cameras_expected": 81, "cameras_missing": 38,
    }
    assert _current_recording_pipeline(json.dumps(snapshot), now=1_000) == snapshot


@pytest.mark.parametrize("timestamp", ["NaN", 10**20, True])
def test_proof_of_life_rejects_heartbeat_without_current_timestamp(monkeypatch, timestamp):
    frame_buffer = Mock()
    frame_buffer.r.get.return_value = json.dumps({
        "last_run_ts": timestamp, "cameras_total": 1, "cameras_fresh": 1,
        "critical_cameras_overdue": 0, "standard_cameras_overdue": 0,
    })
    monkeypatch.setattr("app.api.system.FrameBuffer", lambda: frame_buffer)
    db = Mock()
    db.query.return_value.outerjoin.return_value.order_by.return_value.first.return_value = None
    db.query.return_value.scalar.return_value = None

    payload = proof_of_life(db=db, _u=None)

    assert payload["state"] == "offline"
    assert payload["pipeline_age_seconds"] is None
