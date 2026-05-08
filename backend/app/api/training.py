"""AI Training Studio API.

Endpoints (spec §7):
  POST   /training/datasets/create
  GET    /training/datasets
  GET    /training/datasets/{id}
  POST   /training/images/upload                 — multipart
  POST   /training/images/capture                — grab frame(s) from a live camera
  GET    /training/images/{dataset_id}
  GET    /training/images/{dataset_id}/{image_id}/file       — raw image bytes
  POST   /training/images/{image_id}/auto-suggest            — pre-label with base model
  POST   /training/annotate                                  — save annotations
  POST   /training/jobs/start
  GET    /training/jobs/{id}/status
  GET    /training/jobs/{id}/metrics
  POST   /training/models/{id}/deploy
  POST   /training/models/{id}/rollback
  POST   /training/models/{id}/export
  GET    /training/models
"""
from __future__ import annotations
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile,
                     Form)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.connectors.rtsp import grab_thumbnail
from app.database import get_db
from app.deps import require_role
from app.models import (
    AIModel, Annotation, Camera, Dataset, TrainingImage, TrainingJob,
)
from app.schemas.training import (
    AIModelOut, AnnotationIn, AnnotationOut, CaptureFromCameraIn,
    DatasetCreate, DatasetOut, DeployModelIn, ExportModelIn,
    TrainingImageOut, TrainingJobIn, TrainingJobOut,
)
from app.training.dataset import save_uploaded_image
from app.utils.crypto import decrypt
from app.utils.network import build_rtsp_url

log = logging.getLogger(__name__)
router = APIRouter(prefix="/training", tags=["training"])


# ---------- datasets -------------------------------------------------

@router.post("/datasets/create", response_model=DatasetOut)
def create_dataset(payload: DatasetCreate, db: Session = Depends(get_db),
                   _u=Depends(require_role("admin", "operator"))):
    ds = Dataset(name=payload.name, description=payload.description,
                 classes_json=payload.classes)
    db.add(ds); db.commit(); db.refresh(ds)
    return ds


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db),
                  _u=Depends(require_role("admin", "operator", "viewer"))):
    return db.query(Dataset).order_by(Dataset.id.desc()).all()


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: int, db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator", "viewer"))):
    ds = db.get(Dataset, dataset_id)
    if not ds:
        raise HTTPException(404, "dataset not found")
    return ds


# ---------- images ---------------------------------------------------

@router.post("/images/upload", response_model=list[TrainingImageOut])
async def upload_images(
    dataset_id: int = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    if not db.get(Dataset, dataset_id):
        raise HTTPException(404, "dataset not found")
    out: list[TrainingImage] = []
    for f in files:
        data = await f.read()
        path = save_uploaded_image(dataset_id, f.filename or "image.jpg", data)
        img = TrainingImage(dataset_id=dataset_id, file_path=str(path))
        db.add(img); out.append(img)
    db.commit()
    for r in out:
        db.refresh(r)
    return out


@router.post("/images/capture", response_model=list[TrainingImageOut])
async def capture_frames(payload: CaptureFromCameraIn, db: Session = Depends(get_db),
                         _u=Depends(require_role("admin", "operator"))):
    cam = db.get(Camera, payload.camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    if not db.get(Dataset, payload.dataset_id):
        raise HTTPException(404, "dataset not found")
    pw = decrypt(cam.password_encrypted or "")
    rtsp = build_rtsp_url(brand=cam.brand, host=cam.host, port=cam.rtsp_port,
                          username=cam.username, password=pw,
                          channel=cam.channel_number,
                          override=cam.rtsp_url_override, subtype=0)

    out: list[TrainingImage] = []
    for i in range(max(1, payload.count)):
        b64 = await grab_thumbnail(rtsp, timeout=15)
        if not b64:
            continue
        data = base64.b64decode(b64)
        fname = f"cam{cam.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        path = save_uploaded_image(payload.dataset_id, fname, data)
        img = TrainingImage(
            dataset_id=payload.dataset_id, camera_id=cam.id,
            file_path=str(path),
            captured_at=datetime.now(timezone.utc),
        )
        db.add(img); out.append(img)
    db.commit()
    for r in out:
        db.refresh(r)
    return out


@router.get("/images/{dataset_id}", response_model=list[TrainingImageOut])
def list_images(dataset_id: int, db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator", "viewer"))):
    return (db.query(TrainingImage)
              .filter(TrainingImage.dataset_id == dataset_id)
              .order_by(TrainingImage.id.desc())
              .all())


@router.get("/images/{dataset_id}/{image_id}/file")
def get_image_file(dataset_id: int, image_id: int, db: Session = Depends(get_db),
                   _u=Depends(require_role("admin", "operator", "viewer"))):
    img = db.get(TrainingImage, image_id)
    if not img or img.dataset_id != dataset_id:
        raise HTTPException(404, "image not found")
    p = Path(img.file_path)
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(str(p))


@router.post("/images/{image_id}/auto-suggest", response_model=list[AnnotationIn])
def auto_suggest_image(image_id: int, db: Session = Depends(get_db),
                       _u=Depends(require_role("admin", "operator"))):
    img = db.get(TrainingImage, image_id)
    if not img:
        raise HTTPException(404, "image not found")
    # Lazy: pulls numpy/PIL/ultralytics. The slim `api` image doesn't
    # carry those, so this endpoint only works on the worker image.
    from app.training.annotation import auto_suggest
    return [AnnotationIn(class_label=a["class_label"],
                         bbox_json=a["bbox_json"],
                         auto_suggested=True, verified=False)
            for a in auto_suggest(img.file_path)]


# ---------- annotations ----------------------------------------------

@router.post("/annotate", response_model=list[AnnotationOut])
def save_annotations(image_id: int, payload: list[AnnotationIn],
                     db: Session = Depends(get_db),
                     _u=Depends(require_role("admin", "operator"))):
    img = db.get(TrainingImage, image_id)
    if not img:
        raise HTTPException(404, "image not found")
    # Replace existing annotations for this image (simplest correct UX).
    db.query(Annotation).filter(Annotation.image_id == image_id).delete()
    out: list[Annotation] = []
    for a in payload:
        row = Annotation(image_id=image_id, **a.model_dump())
        db.add(row); out.append(row)
    img.labeled = bool(payload)
    db.commit()
    for r in out:
        db.refresh(r)
    return out


# ---------- training jobs --------------------------------------------

@router.post("/jobs/start", response_model=TrainingJobOut)
def start_job(payload: TrainingJobIn, db: Session = Depends(get_db),
              _u=Depends(require_role("admin", "operator"))):
    ds = db.get(Dataset, payload.dataset_id)
    if not ds:
        raise HTTPException(404, "dataset not found")
    job = TrainingJob(
        model_name=payload.model_name,
        dataset_id=payload.dataset_id,
        config_json=payload.model_dump(),
        status="queued",
        total_epochs=payload.epochs,
    )
    db.add(job); db.commit(); db.refresh(job)

    # Queue the celery task (won't actually run until a worker picks it up).
    try:
        from app.tasks.training import run_training_job
        run_training_job.delay(job.id)
    except Exception as e:
        log.warning("celery enqueue failed (job stays 'queued'): %s", e)
    return job


@router.get("/jobs/{job_id}/status", response_model=TrainingJobOut)
def job_status(job_id: int, db: Session = Depends(get_db),
               _u=Depends(require_role("admin", "operator", "viewer"))):
    j = db.get(TrainingJob, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@router.get("/jobs/{job_id}/metrics")
def job_metrics(job_id: int, db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator", "viewer"))):
    j = db.get(TrainingJob, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return {
        "current_epoch": j.current_epoch,
        "total_epochs":  j.total_epochs,
        "best_map50":    j.best_map50,
        "status":        j.status,
        "error":         j.error_message,
    }


# ---------- models ---------------------------------------------------

@router.get("/models", response_model=list[AIModelOut])
def list_models(db: Session = Depends(get_db),
                _u=Depends(require_role("admin", "operator", "viewer"))):
    return db.query(AIModel).order_by(AIModel.id.desc()).all()


@router.post("/models/{model_id}/deploy", response_model=list[int])
def deploy_model(model_id: int, payload: DeployModelIn,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator"))):
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "model not found")
    affected: list[int] = []
    for cid in payload.camera_ids:
        cam = db.get(Camera, cid)
        if cam:
            cam.ai_model_id = model_id
            affected.append(cam.id)
    m.deployed = True
    db.commit()
    return affected


@router.post("/models/{model_id}/rollback")
def rollback_model(model_id: int, db: Session = Depends(get_db),
                   _u=Depends(require_role("admin"))):
    """Find the previous model with the same `name` and re-deploy it."""
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "model not found")
    prev = (db.query(AIModel)
              .filter(AIModel.name == m.name, AIModel.id < m.id)
              .order_by(AIModel.id.desc())
              .first())
    if not prev:
        raise HTTPException(400, "no previous version exists")
    cams = db.query(Camera).filter(Camera.ai_model_id == m.id).all()
    for c in cams:
        c.ai_model_id = prev.id
    m.deployed = False
    prev.deployed = True
    db.commit()
    return {"rolled_back_to": prev.id, "cameras": [c.id for c in cams]}


@router.post("/models/{model_id}/export")
def export_model(model_id: int, payload: ExportModelIn,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator"))):
    if not db.get(AIModel, model_id):
        raise HTTPException(404, "model not found")
    from app.training.exporter import export
    out = export(model_id, payload.format)
    return {"path": out}
