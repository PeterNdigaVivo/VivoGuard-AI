"""Schemas for the AI Training Studio."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class DatasetCreate(BaseModel):
    name: str
    description: str | None = None
    classes: list[str] = Field(default_factory=list)


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    classes_json: list[str]
    created_at: datetime


class TrainingImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    dataset_id: int
    camera_id: int | None
    file_path: str
    width: int | None
    height: int | None
    captured_at: datetime | None
    labeled: bool
    split: str | None
    source_kind: str
    eligible_for_training: bool
    review_state: str
    simulation_run_id: str | None
    evidence_source: str | None
    synthetic: bool | None


class AnnotationIn(BaseModel):
    class_label: str
    bbox_json: list[float]                # [cx, cy, w, h] normalised
    verified: bool = True
    auto_suggested: bool = False


class AnnotationOut(AnnotationIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    image_id: int


class SimulationEvidenceReviewIn(BaseModel):
    verdict: str = Field(pattern="^(approve|reject)$")
    rationale: str = Field(min_length=8, max_length=2000)


class TrainingJobIn(BaseModel):
    model_name: str
    dataset_id: int
    base_model: str = "yolov8n.pt"
    epochs: int = 50
    batch: int = 16
    imgsz: int = 640
    lr0: float | None = None
    augment: bool = True
    split_train: float = 0.7
    split_val: float = 0.2
    split_test: float = 0.1


class TrainingJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    model_name: str
    dataset_id: int
    config_json: dict
    status: str
    current_epoch: int
    total_epochs: int
    best_map50: float | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class AIModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: str
    base_model: str
    classes_json: list[str]
    map50: float | None
    map50_95: float | None
    precision: float | None
    recall: float | None
    weights_path: str
    export_format: str | None
    deployed: bool
    is_base: bool
    training_job_id: int | None
    created_at: datetime
    # Chain-training metadata (P5). Nullable so legacy per-store models
    # serialise without changes.
    is_chain_model: bool = False
    detector_type: str | None = None
    trained_on_stores: list[int] | None = None
    sample_count: int | None = None


class CaptureFromCameraIn(BaseModel):
    camera_id: int
    dataset_id: int
    count: int = 1                  # number of frames to grab


class DeployModelIn(BaseModel):
    camera_ids: list[int]


class ExportModelIn(BaseModel):
    format: str                     # torchscript | onnx | engine
