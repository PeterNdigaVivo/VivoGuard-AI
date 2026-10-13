import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.ai.inference_worker import _load_camera_state, _zone_list


def test_zone_list_keeps_current_list_payload():
    payload = [[0.1, 0.2], [0.8, 0.2], [0.8, 0.9], [0.1, 0.9]]

    assert _zone_list(payload, zone_id=7, field="polygon_coords_json") is payload


def test_zone_list_decodes_legacy_json_string_payloads():
    coords = [[0.1, 0.2], [0.8, 0.2], [0.8, 0.9], [0.1, 0.9]]

    assert _zone_list(
        json.dumps(coords), zone_id=7, field="polygon_coords_json"
    ) == coords
    assert _zone_list(
        json.dumps(["counter"]), zone_id=7, field="detection_types_json"
    ) == ["counter"]


def test_zone_list_rejects_invalid_or_non_list_payloads(caplog):
    assert _zone_list("not-json", zone_id=7, field="polygon_coords_json") == []
    assert _zone_list('{"x": 0.1}', zone_id=8, field="polygon_coords_json") == []

    assert "zone 7 has invalid polygon_coords_json payload" in caplog.text
    assert "zone 8 has invalid polygon_coords_json payload" in caplog.text


def test_load_camera_state_normalizes_legacy_zone_payloads():
    camera = SimpleNamespace(ai_enabled=True, ai_model_id=None)
    zone = SimpleNamespace(
        id=11,
        name="legacy rectangle",
        shape="rect",
        polygon_coords_json=json.dumps(
            [[0.1, 0.2], [0.8, 0.2], [0.8, 0.9], [0.1, 0.9]]
        ),
        detection_types_json=json.dumps(["counter"]),
        active_schedule_json=None,
        suppressed=False,
    )
    db = MagicMock()
    db.get.return_value = camera
    db.query.return_value.filter.return_value.__iter__.side_effect = [
        iter([zone]),
        iter([]),
    ]

    loaded_camera, zones, _, _ = _load_camera_state(db, camera_id=3)

    assert loaded_camera is camera
    assert zones[0]["polygon_coords_json"] == [
        [0.1, 0.2],
        [0.8, 0.2],
        [0.8, 0.9],
        [0.1, 0.9],
    ]
    assert zones[0]["detection_types_json"] == ["counter"]
