"""Per-camera detection configuration API.

  GET   /cameras/{id}/detection-config         — list all
  POST  /cameras/{id}/detection-config         — bulk upsert (one or many)
  DELETE /cameras/{id}/detection-config/{type} — remove a single type
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_role
from app.models import Camera, DetectionConfig, DETECTION_TYPES
from app.schemas.detection import DetectionConfigIn, DetectionConfigOut

router = APIRouter(prefix="/cameras", tags=["detection"])


# Per-detector default knobs. The GET endpoint synthesises a virtual
# DetectionConfig row from these when no DB row exists for a given
# (camera, detection_type) — so out-of-the-box every camera has every
# detector ENABLED with sensible thresholds, and the AI Settings page
# shows pre-checked boxes instead of all-unchecked.
#
# Threshold rationale (May-2026 spec):
#   occupancy      → alert >80% of store capacity
#   queue          → alert when >3 people waiting
#   dwell          → alert when track lingers >5 min in an aisle zone
#   staff_present  → alert when counter unstaffed >5 min
# Security detectors (intrusion / fight / weapon / fall / fire /
# smoke / abandoned_object / trespass / tailgating / loitering /
# shrinkage / stockroom_access / crowd / shutter-out-of-hours) get a
# zero-threshold default — they alert on every event.
#
# Detectors in the SKIP_ALERTS set don't post operator alerts at all:
#   heatmap            — pure footfall grid, no events
#   customer_journey   — path metric, no events
#   unique_visitor     — daily dedup metric, no events
#   entry_exit         — flow counter, the metrics tell the story
# They still RUN (enabled by default for metrics), they just don't
# clutter the Alerts feed.
SKIP_ALERTS: set[str] = {
    "heatmap", "customer_journey", "unique_visitor", "entry_exit",
    "demographic",   # privacy-compliant bucket counter
}

_DEFAULTS: dict[str, dict] = {
    # threshold detectors — defaults match the spec exactly
    "occupancy":         {"confidence_threshold": 0.6, "extra": {"capacity_pct_threshold": 80}},
    "queue":             {"confidence_threshold": 0.6, "crowd_threshold": 3},
    "dwell":             {"confidence_threshold": 0.5, "dwell_time_seconds": 300},
    "staff_present":     {"confidence_threshold": 0.5, "extra": {"unstaffed_min_threshold": 5}},
    # security detectors — alert immediately
    "intrusion":         {"confidence_threshold": 0.5},
    "trespass":          {"confidence_threshold": 0.5},
    "fight":             {"confidence_threshold": 0.6},
    "fall":              {"confidence_threshold": 0.6},
    "weapon":            {"confidence_threshold": 0.5},
    "weapon_brandished": {"confidence_threshold": 0.5},
    "fire":              {"confidence_threshold": 0.5},
    "smoke":             {"confidence_threshold": 0.5},
    "abandoned_object":  {"confidence_threshold": 0.5, "dwell_time_seconds": 60},
    "loitering":         {"confidence_threshold": 0.5, "dwell_time_seconds": 600},
    "tailgating":        {"confidence_threshold": 0.6},
    "shrinkage":         {"confidence_threshold": 0.5},
    "stockroom_access":  {"confidence_threshold": 0.5},
    "crowd":             {"confidence_threshold": 0.5, "crowd_threshold": 8},
    "shutter":           {"confidence_threshold": 0.5},
    "tripwire":          {"confidence_threshold": 0.5},
    # metrics-only / silent — enabled but skipped from alerts
    "heatmap":           {"confidence_threshold": 0.5},
    "customer_journey":  {"confidence_threshold": 0.5},
    "unique_visitor":    {"confidence_threshold": 0.5},
    "entry_exit":        {"confidence_threshold": 0.5},
    "demographic":       {"confidence_threshold": 0.5},
    # generic / training-required detectors
    "person":            {"confidence_threshold": 0.5},
    "vehicle":           {"confidence_threshold": 0.5},
    "animal":            {"confidence_threshold": 0.5},
    "lpr":               {"confidence_threshold": 0.5},
    "face":              {"confidence_threshold": 0.6},
    "shelf":             {"confidence_threshold": 0.5},
    "shelf_change":      {"confidence_threshold": 0.5},
    "passersby":         {"confidence_threshold": 0.5},
    "window_engagement": {"confidence_threshold": 0.5, "dwell_time_seconds": 5},
    "uniform_compliance":{"confidence_threshold": 0.5},
    "custom":            {"confidence_threshold": 0.5},
}


def _default_row(camera_id: int, detection_type: str) -> dict:
    """Build a virtual DetectionConfig dict for a detector that has no
    DB row yet. The frontend renders this as a pre-checked, sensibly-
    configured detector — operators no longer have to enable each
    detector by hand."""
    base = {
        "id": None,           # virtual — no DB id yet
        "camera_id": camera_id,
        "detection_type": detection_type,
        "enabled": True,      # ← key bit: pre-checked by default
        "confidence_threshold": 0.6,
        "detection_every_n_frames": 1,
        "dwell_time_seconds": None,
        "crowd_threshold": None,
        "schedule_json": None,
        "extra": None,
    }
    base.update(_DEFAULTS.get(detection_type, {}))
    return base


@router.get("/{camera_id}/detection-config", response_model=list[DetectionConfigOut])
def get_detection_config(camera_id: int, db: Session = Depends(get_db),
                         _u=Depends(require_role("admin", "operator", "viewer"))):
    """Return every detector type for this camera.

    Tier 1 — a real DetectionConfig row exists → return as-is.
    Tier 2 — no row exists → return a virtual default with enabled=true
             and the spec's thresholds plugged in.

    The AI Settings page renders the union of both, so out-of-the-box
    every camera has every detector enabled. Operators who EXPLICITLY
    save a disabled config still keep that disabled — virtual rows
    are only synthesised when no row exists at all.
    """
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    existing = {
        r.detection_type: r
        for r in db.query(DetectionConfig).filter(DetectionConfig.camera_id == camera_id).all()
    }
    out: list = []
    for det_type in DETECTION_TYPES:
        if det_type in existing:
            out.append(existing[det_type])
        else:
            out.append(_default_row(camera_id, det_type))
    return out


@router.post("/{camera_id}/detection-config", response_model=list[DetectionConfigOut])
def upsert_detection_config(camera_id: int, payload: list[DetectionConfigIn],
                            db: Session = Depends(get_db),
                            _u=Depends(require_role("admin", "operator"))):
    if not db.get(Camera, camera_id):
        raise HTTPException(404, "camera not found")
    out: list[DetectionConfig] = []
    for item in payload:
        if item.detection_type not in DETECTION_TYPES:
            raise HTTPException(400, f"unknown detection_type: {item.detection_type}")
        row = (db.query(DetectionConfig)
                 .filter(DetectionConfig.camera_id == camera_id,
                         DetectionConfig.detection_type == item.detection_type)
                 .first())
        if not row:
            row = DetectionConfig(camera_id=camera_id, detection_type=item.detection_type)
            db.add(row)
        for k, v in item.model_dump(exclude_unset=True).items():
            setattr(row, k, v)
        out.append(row)
    db.commit()
    for r in out:
        db.refresh(r)
    return out


@router.delete("/{camera_id}/detection-config/{detection_type}")
def delete_detection_config(camera_id: int, detection_type: str,
                            db: Session = Depends(get_db),
                            _u=Depends(require_role("admin", "operator"))):
    row = (db.query(DetectionConfig)
             .filter(DetectionConfig.camera_id == camera_id,
                     DetectionConfig.detection_type == detection_type)
             .first())
    if not row:
        raise HTTPException(404, "config not found")
    db.delete(row)
    db.commit()
    return {"deleted": detection_type}
