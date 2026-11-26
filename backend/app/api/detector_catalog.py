"""Detector catalog — what each detector is really doing across the fleet.

The static table below records CAPABILITY only (runs on the base model vs
needs a trained model) and how to set a detector up. The operator-facing
state (Available / Configured / Validated / Review-only / Training
required / Disabled), camera counts, zone and schedule requirements,
review accuracy and the next action are computed from live configuration
by app.services.detector_status. "status" here is never shown as
"Active": a capability is not proof that anything is running.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.zone_purposes import ZONE_PURPOSES

router = APIRouter(prefix="/detectors", tags=["detectors"])


# Hard-coded status table — mirror the detector implementations.
DETECTOR_CATALOG = {
    # --- Active with default yolov8n.pt ----------------------------------
    "occupancy":           {"status": "active",   "needs_zone": False, "purpose": None},
    "occupancy_metrics":   {"status": "active",   "needs_zone": False, "purpose": None},
    "heatmap":             {"status": "active",   "needs_zone": False, "purpose": None},
    "unique_visitor":      {"status": "active",   "needs_zone": False, "purpose": None},
    "customer_journey":    {"status": "active",   "needs_zone": False, "purpose": None},
    "crowd":               {"status": "active",   "needs_zone": False, "purpose": None},
    "fall":                {"status": "active",   "needs_zone": False, "purpose": None},
    "abandoned_object":    {"status": "active",   "needs_zone": False, "purpose": None},
    "person":              {"status": "active",   "needs_zone": False, "purpose": None},
    "vehicle":             {"status": "active",   "needs_zone": False, "purpose": None},
    "animal":              {"status": "active",   "needs_zone": False, "purpose": None},
    "fight":               {"status": "active",   "needs_zone": False, "purpose": None},

    "queue":               {"status": "active",   "needs_zone": True,  "purpose": "checkout_queue"},
    "staff_present":       {"status": "active",   "needs_zone": True,  "purpose": "staff_counter"},
    "dwell":               {"status": "active",   "needs_zone": True,  "purpose": "product_aisle"},
    "entry_exit":          {"status": "active",   "needs_zone": True,  "purpose": "count_entries"},
    "intrusion":           {"status": "active",   "needs_zone": True,  "purpose": "intrusion_zone"},
    # Needs a zone tagged "trespass" (the intrusion purpose only tags
    # "restricted", which drives intrusion, not trespass).
    "trespass":            {"status": "active",   "needs_zone": True,  "purpose": None},
    "stockroom_access":    {"status": "active",   "needs_zone": True,  "purpose": "stockroom"},
    "shutter":             {"status": "active",   "needs_zone": True,  "purpose": "shutter"},
    "shrinkage":           {"status": "active",   "needs_zone": True,  "purpose": "high_value"},
    "passersby":           {"status": "active",   "needs_zone": True,  "purpose": "sidewalk"},
    "window_engagement":   {"status": "active",   "needs_zone": True,  "purpose": "window_display"},
    "loitering":           {"status": "active",   "needs_zone": True,  "purpose": None},
    "tripwire":            {"status": "active",   "needs_zone": True,  "purpose": None},
    "tailgating":          {"status": "active",   "needs_zone": True,  "purpose": None},
    "shelf_change":        {"status": "active",   "needs_zone": True,  "purpose": "shelf"},

    # Uniform compliance works rule-based now (colour analysis of the
    # upper body in a counter/staff zone) — accuracy improves with a
    # trained per-store model.
    "staff_zone":          {"status": "active", "needs_zone": True, "purpose": "staff_zone",
                            "label": "Staff Area (Behind Counter)",
                            "description": "Staff-only area behind the service counter. Alerts when a non-uniformed person enters."},
    "checkout_dwell":      {"status": "active",   "needs_zone": True,  "purpose": "staff_counter"},
    "uniform_compliance":  {"status": "active", "needs_zone": True, "purpose": "staff_counter",
                            "training_hint": "Works out of the box via colour analysis. For best accuracy, collect frames at /training/shutter-style Uniform tab and train a YOLOv8n-cls model (uniform_ok / uniform_violation / no_lanyard / civilian)."},

    # --- Training required ------------------------------------------------
    "demographic":         {"status": "training_required", "needs_zone": False,
                            "training_hint": "Train a model emitting 'age_<bucket>_gender_<m|f|u>' classes. Falls back to an 'unknown' aggregate bucket without a model."},
    "face":                {"status": "training_required", "needs_zone": False,
                            "training_hint": "Default YOLOv8 has no 'face' class. Train or load a face-detection model."},
    "weapon":              {"status": "training_required", "needs_zone": False,
                            "training_hint": "Default COCO has no 'weapon' class. Train with classes like 'gun', 'knife'."},
    "weapon_brandished":   {"status": "training_required", "needs_zone": False,
                            "training_hint": "Needs a model emitting 'weapon_drawn' / 'gun_brandished' classes."},
    "fire":                {"status": "training_required", "needs_zone": False,
                            "training_hint": "Train a model emitting 'fire' / 'flame' classes."},
    "smoke":               {"status": "training_required", "needs_zone": False,
                            "training_hint": "Train a model emitting 'smoke' class."},
    "shelf":               {"status": "training_required", "needs_zone": False,
                            "training_hint": "Train with classes 'shelf_empty' / 'out_of_stock'. Distinct from the shelf-change image-diff detector."},
    "lpr":                 {"status": "active",   "needs_zone": False,
                            "training_hint": "Plate reading needs the OCR component in the worker image; without it, vehicles are still logged."},
    "custom":              {"status": "training_required", "needs_zone": False,
                            "training_hint": "Bring any custom YOLOv8 model and select it on the camera."},
}


@router.get("/catalog")
def catalog(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Per detector: label, set-up purpose, capability, and the computed
    fleet state with the numbers and next action an operator needs.
    Build/implementation notes are returned to administrators only."""
    from app.services.detector_status import compute, gather

    data = gather(db)
    is_admin = getattr(user, "role", None) == "admin"
    out: dict = {}
    for dt, meta in DETECTOR_CATALOG.items():
        row = {
            "label": meta.get("label"),
            "description": meta.get("description"),
            "purpose": meta.get("purpose"),
            "purpose_label": (ZONE_PURPOSES.get(meta["purpose"]) or {}).get("label")
                             if meta.get("purpose") else None,
            "capability": meta.get("status"),
            **compute(dt, meta, data),
        }
        if is_admin and meta.get("training_hint"):
            row["admin_note"] = meta["training_hint"]
        out[dt] = row
    return out
