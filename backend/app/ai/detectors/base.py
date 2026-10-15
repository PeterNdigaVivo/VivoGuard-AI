"""Detector base class and the canonical event payload shape."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# COCO class names → grouped logical buckets used by the detectors.
COCO_PERSON   = {"person"}
COCO_VEHICLE  = {"car", "truck", "motorcycle", "motorbike", "bicycle", "bus"}
COCO_ANIMAL   = {"cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "bird"}
# Custom-trained models override these via class names.


@dataclass
class DetectionEvent:
    """A single detection that crosses thresholds and enters the system."""

    detection_type: str            # e.g. "person", "fire", "loitering"
    cls: str                       # underlying class label from the model
    confidence: float
    bbox_norm: list[float]         # [x1, y1, x2, y2] in [0..1]
    track_id: int | None = None
    zone_id: int | None = None     # filled by the zone evaluator
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectorContext:
    """Per-frame context passed to every detector."""

    camera_id: int
    timestamp: float
    raw_detections: list[dict]              # full YOLO output
    tracks: list[tuple]                     # (Track, det) pairs from IOUTracker
    zones: list[dict]                       # zone rows ({id, polygon_coords_json, detection_types_json, ...})
    config: dict                            # detection_configs keyed by type
    # Retail extension (commit 0+). Optional so older callers keep working.
    db: Any = None                          # SQLAlchemy session for metric writes
    store_id: int | None = None             # cached camera.store_id
    business_hours: dict | None = None      # cached store.business_hours_json
    store_timezone: str = "UTC"
    # Raw BGR ndarray for detectors that need pixel access (e.g. the
    # ShutterDetector luminance heuristic). Inference worker sets this;
    # if absent, pixel-mode detectors fall through.
    frame_bgr: Any = None
    # Track ids the mannequin filter judged stationary this frame.
    #
    # The worker already computes these to build the static-filtered
    # context, then discards them. Staff and compliance detectors
    # deliberately receive the UNfiltered context -- a cashier standing
    # still at a till is a real person they must see -- but some of
    # their rules are accusations rather than observations, and those
    # should not fire on something that has never moved. Camera 73
    # reported "unauthorised person" 58 times in a week from an
    # identical bounding box pinned to the top of the frame: a pillar.
    # Carrying the ids lets a detector keep seeing a static track while
    # declining to accuse it.
    static_track_ids: frozenset = frozenset()


class Detector:
    """Subclass and implement `evaluate(ctx) -> list[DetectionEvent]`."""

    detection_type: str = "base"
    needs_tracking: bool = False

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:    # noqa: ARG002
        raise NotImplementedError
