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
from app.ai.detectors.retail_p2 import (
    AisleDwellDetector, ShutterDetector, StaffPresenceDetector,
)
from app.ai.detectors.retail_p3 import (
    DemographicDetector, ShrinkageDetector, StockroomAccessDetector,
    UniformComplianceDetector,
)
from app.ai.detectors.retail_p4 import (
    PassersbyDetector, WindowEngagementDetector,
)


# Stateless — one shared instance per type, built from class refs.
# `d` is the class; `d()` instantiates; `d.detection_type` is the class
# attribute defined on each Detector subclass.
STATELESS: dict[str, Detector] = {
    d.detection_type: d() for d in (
        PersonDetector, VehicleDetector, AnimalDetector, FaceDetector,
        WeaponDetector, WeaponBrandishedDetector, FireDetector, SmokeDetector,
        ShelfDetector, CustomDetector, CrowdDetector, OccupancyDetector,
        TrespassDetector, FallDetector,
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
    # Retail P2.
    "staff_present":      StaffPresenceDetector,
    "dwell":              AisleDwellDetector,
    "shutter":            ShutterDetector,
    # Retail P3.
    "uniform_compliance": UniformComplianceDetector,
    "demographic":        DemographicDetector,
    "shrinkage":          ShrinkageDetector,
    "stockroom_access":   StockroomAccessDetector,
    # Retail P4.
    "passersby":          PassersbyDetector,
    "window_engagement":  WindowEngagementDetector,
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
