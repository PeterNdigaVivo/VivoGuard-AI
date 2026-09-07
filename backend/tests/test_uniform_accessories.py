"""Accessory rules must override stale uniform-model predictions."""
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from app.ai.detectors.uniform_compliance import (
    FULL_COMPLIANT,
    UniformComplianceDetector,
    uniform_features,
)


def _compliant_staff_frame(*, badge: bool = True):
    frame = np.full((240, 120, 3), 220, dtype=np.uint8)
    frame[:130, :] = (25, 25, 25)  # black uniform top
    frame[130:, :] = (20, 20, 20)  # black trousers
    orange = (0, 140, 255)
    cv2.line(frame, (48, 35), (60, 105), orange, 4)
    cv2.line(frame, (72, 35), (60, 105), orange, 4)
    if badge:
        cv2.rectangle(frame, (48, 82), (73, 96), (245, 245, 245), -1)
    return frame


def test_orange_lanyard_and_white_card_are_valid_nametag() -> None:
    features = uniform_features(_compliant_staff_frame(), [0, 0, 1, 1])

    assert features is not None
    assert features["top_ok"] is True
    assert features["has_lanyard"] is True
    assert features["has_nametag"] is True


def test_visible_accessories_override_no_lanyard_model_prediction() -> None:
    frame = _compliant_staff_frame()
    detection = {"cls": "person", "conf": 0.9, "bbox_norm": [0, 0, 1, 1]}
    context = SimpleNamespace(
        frame_bgr=frame,
        raw_detections=[
            {"cls": "no_lanyard", "conf": 0.99, "bbox_norm": [0, 0, 1, 1]},
        ],
    )

    state = UniformComplianceDetector()._classify(
        context, detection, {"confidence_threshold": 0.5, "extra": {}},
    )

    assert state == FULL_COMPLIANT


def test_orange_lanyard_alone_overrides_missing_tag_prediction() -> None:
    frame = _compliant_staff_frame(badge=False)
    detection = {"cls": "person", "conf": 0.9, "bbox_norm": [0, 0, 1, 1]}
    context = SimpleNamespace(
        frame_bgr=frame,
        raw_detections=[
            {"cls": "no_lanyard", "conf": 0.99, "bbox_norm": [0, 0, 1, 1]},
        ],
    )

    features = uniform_features(frame, detection["bbox_norm"])
    state = UniformComplianceDetector()._classify(
        context, detection, {"confidence_threshold": 0.5, "extra": {}},
    )

    assert features is not None
    assert features["has_lanyard"] is True
    assert features["has_nametag"] is False
    assert state == FULL_COMPLIANT
