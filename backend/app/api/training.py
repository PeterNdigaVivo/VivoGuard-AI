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


# ====================================================================
# Shutter / door-status classification training data collection
# ====================================================================
# A classification workflow (one label per whole frame) distinct from
# the bbox dataset flow above. Operators capture frames from a camera
# that has a 'shutter' zone and tag each OPEN / CLOSED / PARTIAL. The
# frames feed the YOLOv8n-cls training pipeline (separate commit).

SHUTTER_LABELS = {"open", "closed", "partial"}


def _shutter_sample_root(label: str, camera_id: int) -> Path:
    from app.config import settings
    p = Path(settings.datasets_dir).parent / "training" / "shutter" / label
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/shutter/cameras")
def shutter_cameras(db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator", "viewer"))):
    """Cameras that have a 'shutter'-tagged zone — the only ones worth
    collecting shutter training data on. Returns id, name, store, and
    the current per-label sample counts so the UI can show progress."""
    from app.models import Zone, Store, TrainingSample
    from sqlalchemy import func
    cam_ids = [
        z.camera_id for z in db.query(Zone).all()
        if "shutter" in (z.detection_types_json or []) and z.camera_id
    ]
    cam_ids = sorted(set(cam_ids))
    if not cam_ids:
        return []
    counts: dict[tuple[int, str], int] = {}
    for cam_id, label, n in (
        db.query(TrainingSample.camera_id, TrainingSample.label, func.count(TrainingSample.id))
          .filter(TrainingSample.detector_type == "shutter",
                  TrainingSample.camera_id.in_(cam_ids))
          .group_by(TrainingSample.camera_id, TrainingSample.label).all()
    ):
        counts[(cam_id, label)] = int(n)
    out = []
    for cam in db.query(Camera).filter(Camera.id.in_(cam_ids)).all():
        store = db.get(Store, cam.store_id) if cam.store_id else None
        out.append({
            "camera_id": cam.id,
            "camera_name": cam.name,
            "store_id": cam.store_id,
            "store_name": store.name if store else None,
            "counts": {
                "open":    counts.get((cam.id, "open"), 0),
                "closed":  counts.get((cam.id, "closed"), 0),
                "partial": counts.get((cam.id, "partial"), 0),
            },
        })
    return out


@router.post("/shutter/capture")
async def shutter_capture(camera_id: int, label: str,
                          db: Session = Depends(get_db),
                          user=Depends(require_role("admin", "operator"))):
    """Grab the current frame from `camera_id` and store it tagged
    `label` (open|closed|partial). Uses the streamer's cached JPEG
    first, falling back to a one-shot RTSP pull."""
    import base64
    from datetime import datetime, timezone
    from app.models import TrainingSample
    from app.stream.frame_buffer import FrameBuffer

    label = (label or "").lower().strip()
    if label not in SHUTTER_LABELS:
        raise HTTPException(400, f"label must be one of {sorted(SHUTTER_LABELS)}")
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    data = FrameBuffer().latest_jpeg(camera_id)
    if not data:
        pw = decrypt(cam.password_encrypted or "")
        rtsp = build_rtsp_url(brand=cam.brand, host=cam.host, port=cam.rtsp_port,
                              username=cam.username, password=pw,
                              channel=cam.channel_number,
                              override=cam.rtsp_url_override, subtype=0)
        b64 = await grab_thumbnail(rtsp, timeout=15)
        if not b64:
            raise HTTPException(503, "could not grab a frame from this camera")
        data = base64.b64decode(b64)

    ts = datetime.now(timezone.utc)
    fname = f"{camera_id}_{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    path = _shutter_sample_root(label, camera_id) / fname
    path.write_bytes(data)
    sample = TrainingSample(
        detector_type="shutter", label=label,
        camera_id=camera_id, store_id=cam.store_id,
        frame_path=str(path), captured_at=ts,
        labeled_by=getattr(user, "id", None),
    )
    db.add(sample); db.commit(); db.refresh(sample)
    return {"id": sample.id, "label": label, "captured_at": ts.isoformat()}


@router.get("/shutter/samples")
def shutter_samples(camera_id: int, label: str | None = None,
                    db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator", "viewer"))):
    """List captured shutter samples for a camera, newest first."""
    from app.models import TrainingSample
    q = (db.query(TrainingSample)
           .filter(TrainingSample.detector_type == "shutter",
                   TrainingSample.camera_id == camera_id))
    if label:
        q = q.filter(TrainingSample.label == label.lower())
    return [
        {"id": s.id, "label": s.label,
         "captured_at": s.captured_at.isoformat() if s.captured_at else None,
         "file_url": f"/api/training/shutter/samples/{s.id}/file"}
        for s in q.order_by(TrainingSample.id.desc()).limit(500).all()
    ]


@router.get("/shutter/samples/{sample_id}/file")
def shutter_sample_file(sample_id: int, db: Session = Depends(get_db),
                        _u=Depends(require_role("admin", "operator", "viewer"))):
    from app.models import TrainingSample
    s = db.get(TrainingSample, sample_id)
    if not s:
        raise HTTPException(404, "sample not found")
    p = Path(s.frame_path)
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(str(p))


@router.delete("/shutter/samples/{sample_id}")
def shutter_sample_delete(sample_id: int, db: Session = Depends(get_db),
                          _u=Depends(require_role("admin", "operator"))):
    from app.models import TrainingSample
    s = db.get(TrainingSample, sample_id)
    if not s:
        raise HTTPException(404, "sample not found")
    try:
        Path(s.frame_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.delete(s); db.commit()
    return {"deleted": sample_id}


@router.post("/shutter/train")
def shutter_train_start(store_id: int, camera_id: int | None = None,
                        db: Session = Depends(get_db),
                        _u=Depends(require_role("admin", "operator"))):
    """Kick off YOLOv8n-cls training for a store's shutter samples.
    Requires ≥50 frames per class. Runs as a background Celery task;
    poll /shutter/train/status."""
    from sqlalchemy import func
    from app.models import TrainingSample
    rows = dict(
        db.query(TrainingSample.label, func.count(TrainingSample.id))
          .filter(TrainingSample.detector_type == "shutter",
                  TrainingSample.store_id == store_id)
          .group_by(TrainingSample.label).all()
    )
    short = {l: int(rows.get(l, 0)) for l in ("open", "closed", "partial")
             if int(rows.get(l, 0)) < 50}
    if short:
        raise HTTPException(
            400,
            f"Need ≥50 frames per class. Short: " +
            ", ".join(f"{k}={v}" for k, v in short.items()))
    from app.tasks.shutter_training import train_shutter_model
    train_shutter_model.delay(store_id, camera_id)
    return {"started": True, "store_id": store_id}


@router.get("/shutter/train/status")
def shutter_train_status(store_id: int,
                         _u=Depends(require_role("admin", "operator", "viewer"))):
    """Latest training status for a store (polled by the UI)."""
    import json
    import redis
    from app.config import settings
    r = redis.from_url(settings.redis_url, decode_responses=True)
    raw = r.get(f"vg:shutter_train:status:{store_id}")
    if not raw:
        return {"state": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"state": "idle"}


@router.post("/shutter/deploy")
def shutter_deploy(model_id: int, camera_ids: list[int],
                   db: Session = Depends(get_db),
                   _u=Depends(require_role("admin", "operator"))):
    """Assign a trained shutter model to the given cameras so the
    ShutterDetector uses it instead of the rule-based path."""
    model = db.get(AIModel, model_id)
    if not model:
        raise HTTPException(404, "model not found")
    updated = []
    for cid in camera_ids:
        cam = db.get(Camera, cid)
        if cam:
            cam.ai_model_id = model_id
            updated.append(cid)
    model.deployed = True
    db.commit()
    return {"deployed_to": updated, "model_id": model_id}
