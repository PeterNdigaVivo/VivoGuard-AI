"""Detector registry — maps detection_type strings to Detector instances.

Stateful detectors are stored *per camera* so trackers don't bleed
between cameras. Stateless detectors are shared globally.
"""
from __future__ import annotations
from typing import Type

from app.ai.detectors.base import Detector
from app.ai.detectors.simple import (
    AnimalDetector, CustomDetector, FaceDetector, FireDetector,
    PersonDetector, ShelfDetector, SmokeDetector, VehicleDetector,
    WeaponBrandishedDetector, WeaponDetector,
)
from app.ai.detectors.stateful import (
    AbandonedObjectDetector, CrowdDetector, FallDetector, HeatmapDetector,
    LPRDetector, LoiteringDetector, OccupancyDetector, TailgatingDetector,
    TrespassDetector, TripwireDetector,
)
from app.ai.detectors.retail_p1 import (
    IntrusionDetector, OccupancyMetricsDetector, QueueDetector,
    UniqueVisitorDetector,
)


# Stateless — safe to share a single instance.
STATELESS: dict[str, Detector] = {
    d.detection_type: d() for d in (
        PersonDetector(), VehicleDetector(), AnimalDetector(), FaceDetector(),
        WeaponDetector(), WeaponBrandishedDetector(), FireDetector(), SmokeDetector(),
        ShelfDetector(), CustomDetector(), CrowdDetector(), OccupancyDetector(),
        TrespassDetector(), FallDetector(),
    )
}

# Stateful — instantiated per camera in DetectorRegistry.
STATEFUL_TYPES: dict[str, Type[Detector]] = {
    "loitering":          LoiteringDetector,
    "abandoned_object":   AbandonedObjectDetector,
    "tripwire":           TripwireDetector,
    "tailgating":         TailgatingDetector,
    "heatmap":            HeatmapDetector,
    "lpr":                LPRDetector,
    # Retail P1.
    "queue":              QueueDetector,
    "occupancy_metrics":  OccupancyMetricsDetector,
    "unique_visitor":     UniqueVisitorDetector,
    "intrusion":          IntrusionDetector,
}


class DetectorRegistry:
    def __init__(self):
        self._per_camera: dict[int, dict[str, Detector]] = {}

    def detectors_for(self, camera_id: int) -> list[Detector]:
        out: list[Detector] = list(STATELESS.values())
        bag = self._per_camera.setdefault(camera_id, {})
        for t, cls in STATEFUL_TYPES.items():
            if t not in bag:
                bag[t] = cls()
            out.append(bag[t])
        return out


__all__ = ["DetectorRegistry", "STATELESS", "STATEFUL_TYPES"]
