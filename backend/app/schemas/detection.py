"""Schemas for detection config and zones."""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field


class DetectionConfigIn(BaseModel):
    detection_type: str
    enabled: bool = False
    confidence_threshold: float = Field(0.6, ge=0.1, le=0.99)
    min_object_size: int = 0
    detection_every_n_frames: int = 1
    dwell_time_seconds: int | None = None
    crowd_threshold: int | None = None
    extra: dict | None = None
    schedule_json: dict | None = None


class DetectionConfigOut(DetectionConfigIn):
    model_config = ConfigDict(from_attributes=True)
    # `id` is None for VIRTUAL rows synthesised by the GET endpoint
    # when no DB row exists yet — keeps the response shape uniform so
    # the frontend renders every detector pre-checked without
    # special-casing the missing-row case.
    id: int | None = None
    camera_id: int


class ZoneIn(BaseModel):
    name: str
    shape: str = "polygon"           # polygon | line
    polygon_coords_json: list[list[float]]
    detection_types_json: list[str] = Field(default_factory=list)
    active_schedule_json: dict | None = None
    suppressed: bool = False


class ZoneOut(ZoneIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    camera_id: int
