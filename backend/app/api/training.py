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


# ---- Drift dashboard + feedback-pool stats --------------------------

@router.get("/model-metrics")
def model_metrics(
    detection_type: str | None = None,
    model_id: int | None       = None,
    days: int                  = 30,
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator", "viewer")),
):
    """Per-day precision + fp_rate per (model, detection_type). The
    chart is computed nightly by the
    `training.compute_model_metrics_daily` task — this endpoint just
    reads. Filterable by `detection_type` and/or `model_id`; default
    window is 30 days."""
    from datetime import date as _date, timedelta as _td
    from app.models import ModelMetric

    days = max(1, min(int(days), 180))
    since = _date.today() - _td(days=days)
    q = db.query(ModelMetric).filter(ModelMetric.day >= since)
    if detection_type is not None:
        q = q.filter(ModelMetric.detection_type == detection_type)
    if model_id is not None:
        q = q.filter(ModelMetric.model_id == model_id)
    rows = q.order_by(ModelMetric.day.asc(),
                      ModelMetric.detection_type.asc(),
                      ModelMetric.model_id.asc().nullslast()).all()
    return [{
        "day":            r.day.isoformat(),
        "model_id":       r.model_id,
        "detection_type": r.detection_type,
        "tp_count":       r.tp_count,
        "fp_count":       r.fp_count,
        "alerts_total":   r.alerts_total,
        "precision":      r.precision,
        "fp_rate":        r.fp_rate,
        "recall":         r.recall,
        "computed_at":    r.computed_at.isoformat() if r.computed_at else None,
    } for r in rows]


@router.post("/model-metrics/refresh")
def refresh_model_metrics(
    days: int = 2,
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    """Manual trigger for a metrics rebuild. The Celery beat already
    runs this every 15 min, but ops may want to force-refresh after
    a triage sweep."""
    from app.training.metrics import backfill_recent_days
    upserts = backfill_recent_days(db, days=max(1, min(int(days), 30)))
    return {"buckets_upserted": upserts, "days": days}


# ---- Pseudo-labelling --------------------------------------------------

@router.post("/pseudo-label/run")
def run_pseudo_label(
    dataset_id: int | None = None,
    conf: float            = 0.75,
    limit: int             = 500,
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    """Manual trigger for the pseudo-labeller. Without dataset_id,
    walks every dataset with pending unlabelled images (same path the
    hourly Celery task uses). Returns a counts summary."""
    from app.training.pseudo_label import (
        pseudo_label_all_pending, pseudo_label_dataset,
    )
    conf = max(0.1, min(0.99, float(conf)))
    limit = max(1, min(int(limit), 5000))
    if dataset_id is None:
        return pseudo_label_all_pending(db, conf=conf,
                                         limit_per_dataset=limit)
    return pseudo_label_dataset(db, int(dataset_id),
                                 conf=conf, limit=limit)


# ---- Incremental fine-tune -------------------------------------------

@router.post("/jobs/start-finetune", response_model=TrainingJobOut)
def start_finetune_job(
    detection_type: str,
    resume_from_model_id: int,
    epochs: int          = 10,
    batch:  int          = 16,
    imgsz:  int          = 640,
    lr0:    float        = 0.0005,
    max_neg_ratio: float = 3.0,
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator")),
):
    """Queue an incremental fine-tune. Reads positives from the
    `feedback-<detection_type>` Dataset and negatives from the
    paired `feedback-negative-<detection_type>` pool. Starts from
    the `resume_from_model_id` weights with a low LR + short epoch
    schedule (Phase 2 default: 10 epochs, lr0=0.0005)."""
    parent = db.get(AIModel, int(resume_from_model_id))
    if parent is None:
        raise HTTPException(404, f"AIModel id={resume_from_model_id} not found")

    pos_ds = (db.query(Dataset)
                .filter(Dataset.name == f"feedback-{detection_type}")
                .first())
    if pos_ds is None:
        raise HTTPException(
            404, f"no positive feedback pool for detection_type="
                 f"'{detection_type}' (expected dataset name "
                 f"'feedback-{detection_type}')")
    neg_ds = (db.query(Dataset)
                .filter(Dataset.name == f"feedback-negative-{detection_type}")
                .first())
    extra_neg_ids = [neg_ds.id] if neg_ds else []

    cfg = {
        "incremental_finetune":       True,
        "resume_from_model_id":       int(resume_from_model_id),
        "extra_negative_dataset_ids": extra_neg_ids,
        "max_neg_ratio":              float(max_neg_ratio),
        "epochs":                     int(epochs),
        "batch":                      int(batch),
        "imgsz":                      int(imgsz),
        "lr0":                        float(lr0),
        "augment":                    True,
    }
    job = TrainingJob(
        model_name=parent.name,        # next vN auto-assigned at run time
        dataset_id=pos_ds.id,
        config_json=cfg,
        status="queued",
        total_epochs=int(epochs),
    )
    db.add(job); db.commit(); db.refresh(job)

    # Hand off to the same Celery worker the regular `start` endpoint
    # uses — the trainer reads `incremental_finetune` from cfg.
    from app.tasks.training import run_training_job
    run_training_job.delay(job.id)
    return job


@router.get("/feedback-stats")
def feedback_stats(
    detection_type: str | None = None,
    db: Session = Depends(get_db),
    _u=Depends(require_role("admin", "operator", "viewer")),
):
    """How many confirmed/dismissed alerts are sitting in each
    detection type's feedback pool, ready for the next fine-tune.
    When `detection_type` is None, returns a row per type known to
    the system."""
    from app.training.feedback_loop import pending_retraining_count
    from app.models import DetectionEvent
    if detection_type is not None:
        types = [detection_type]
    else:
        types = [t for (t,) in
                 db.query(DetectionEvent.detection_type).distinct().all()
                 if t]
    out = []
    for t in sorted(types):
        counts = pending_retraining_count(db, t)
        out.append({"detection_type": t,
                    "positives": counts["positives"],
                    "negatives": counts["negatives"]})
    return out


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
    """ALL ai_enabled cameras across all stores, with a flag for
    whether each already has a 'shutter'-tagged zone and its current
    per-label sample counts. Operators need to collect frames on
    cameras that don't have a zone yet (they draw the zone after
    training), so we no longer filter to shutter-zoned cameras."""
    from app.models import Zone, Store, TrainingSample
    from sqlalchemy import func

    # Which cameras already have a shutter zone.
    shutter_cam_ids = {
        z.camera_id for z in db.query(Zone).all()
        if "shutter" in (z.detection_types_json or []) and z.camera_id
    }

    cams = (db.query(Camera)
              .filter(Camera.ai_enabled == True)  # noqa: E712
              .all())
    cam_ids = [c.id for c in cams]
    counts: dict[tuple[int, str], int] = {}
    if cam_ids:
        for cam_id, label, n in (
            db.query(TrainingSample.camera_id, TrainingSample.label, func.count(TrainingSample.id))
              .filter(TrainingSample.detector_type == "shutter",
                      TrainingSample.camera_id.in_(cam_ids))
              .group_by(TrainingSample.camera_id, TrainingSample.label).all()
        ):
            counts[(cam_id, label)] = int(n)

    store_names = {s.id: s.name for s in db.query(Store).all()}
    out = []
    for cam in cams:
        out.append({
            "camera_id": cam.id,
            "camera_name": cam.name,
            "store_id": cam.store_id,
            "store_name": store_names.get(cam.store_id) if cam.store_id else None,
            "has_shutter_zone": cam.id in shutter_cam_ids,
            "counts": {
                "open":    counts.get((cam.id, "open"), 0),
                "closed":  counts.get((cam.id, "closed"), 0),
                "partial": counts.get((cam.id, "partial"), 0),
            },
        })
    # Sort by store name then camera name so the grouped dropdown is tidy.
    out.sort(key=lambda c: ((c["store_name"] or "~"), c["camera_name"]))
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

    data = FrameBuffer().latest_jpeg(camera_id, prefer_overlay=False)
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


# ====================================================================
# Uniform-compliance classification training data collection
# ====================================================================
# Same frame→label flow as shutter, with four labels and a 'counter'
# zone for the "already configured" hint. Kept as its own routes for
# clarity; shares the TrainingSample table (detector_type="uniform").

# P5 6-class label set. Legacy 4-class names kept as accepted aliases
# so older clients (and the camera-crop harvester) still work — they
# map to the closest new bucket.
UNIFORM_LABELS = {
    "full_compliant", "partial_compliant", "color_only",
    "non_compliant",  "customer", "uncertain",
    # legacy aliases
    "uniform_ok", "no_lanyard", "uniform_violation", "civilian",
}
UNIFORM_LABEL_ALIASES = {
    "uniform_ok":        "full_compliant",
    "no_lanyard":        "partial_compliant",
    "uniform_violation": "non_compliant",
    "civilian":          "customer",
}


def _uniform_sample_root(label: str) -> Path:
    from app.config import settings
    p = Path(settings.datasets_dir).parent / "training" / "uniform" / label
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/uniform/cameras")
def uniform_cameras(db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator", "viewer"))):
    """All ai_enabled cameras with a 'counter' zone flag + per-label
    sample counts for the uniform detector."""
    from app.models import Zone, Store, TrainingSample
    from sqlalchemy import func

    counter_cam_ids = {
        z.camera_id for z in db.query(Zone).all()
        if ({"counter", "staff"} & set(z.detection_types_json or [])) and z.camera_id
    }
    cams = db.query(Camera).filter(Camera.ai_enabled == True).all()  # noqa: E712
    cam_ids = [c.id for c in cams]
    counts: dict[tuple[int, str], int] = {}
    if cam_ids:
        for cam_id, label, n in (
            db.query(TrainingSample.camera_id, TrainingSample.label, func.count(TrainingSample.id))
              .filter(TrainingSample.detector_type == "uniform",
                      TrainingSample.camera_id.in_(cam_ids))
              .group_by(TrainingSample.camera_id, TrainingSample.label).all()
        ):
            counts[(cam_id, label)] = int(n)
    store_names = {s.id: s.name for s in db.query(Store).all()}
    out = []
    for cam in cams:
        out.append({
            "camera_id": cam.id,
            "camera_name": cam.name,
            "store_id": cam.store_id,
            "store_name": store_names.get(cam.store_id) if cam.store_id else None,
            "has_counter_zone": cam.id in counter_cam_ids,
            "counts": {l: counts.get((cam.id, l), 0) for l in UNIFORM_LABELS},
        })
    out.sort(key=lambda c: ((c["store_name"] or "~"), c["camera_name"]))
    return out


@router.post("/uniform/capture")
async def uniform_capture(camera_id: int, label: str,
                          db: Session = Depends(get_db),
                          user=Depends(require_role("admin", "operator"))):
    """Grab the current frame from `camera_id`, store it tagged `label`
    (uniform_ok|uniform_violation|no_lanyard|civilian)."""
    import base64
    from datetime import datetime, timezone
    from app.models import TrainingSample
    from app.stream.frame_buffer import FrameBuffer

    label = UNIFORM_LABEL_ALIASES.get((label or "").lower().strip(),
                                        (label or "").lower().strip())
    if label not in UNIFORM_LABELS:
        raise HTTPException(400, f"label must be one of {sorted(UNIFORM_LABELS)}")
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    data = FrameBuffer().latest_jpeg(camera_id, prefer_overlay=False)
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
    path = _uniform_sample_root(label) / fname
    path.write_bytes(data)
    sample = TrainingSample(
        detector_type="uniform", label=label,
        camera_id=camera_id, store_id=cam.store_id,
        frame_path=str(path), captured_at=ts,
        labeled_by=getattr(user, "id", None),
    )
    db.add(sample); db.commit(); db.refresh(sample)
    return {"id": sample.id, "label": label, "captured_at": ts.isoformat()}


@router.get("/uniform/samples")
def uniform_samples(camera_id: int, label: str | None = None,
                    db: Session = Depends(get_db),
                    _u=Depends(require_role("admin", "operator", "viewer"))):
    from app.models import TrainingSample
    q = (db.query(TrainingSample)
           .filter(TrainingSample.detector_type == "uniform",
                   TrainingSample.camera_id == camera_id))
    if label:
        q = q.filter(TrainingSample.label == label.lower())
    return [
        {"id": s.id, "label": s.label,
         "captured_at": s.captured_at.isoformat() if s.captured_at else None,
         "file_url": f"/api/training/uniform/samples/{s.id}/file"}
        for s in q.order_by(TrainingSample.id.desc()).limit(500).all()
    ]


@router.get("/uniform/samples/{sample_id}/file")
def uniform_sample_file(sample_id: int, db: Session = Depends(get_db),
                        _u=Depends(require_role("admin", "operator", "viewer"))):
    from app.models import TrainingSample
    s = db.get(TrainingSample, sample_id)
    if not s:
        raise HTTPException(404, "sample not found")
    p = Path(s.frame_path)
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(str(p))


@router.delete("/uniform/samples/{sample_id}")
def uniform_sample_delete(sample_id: int, db: Session = Depends(get_db),
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


@router.post("/uniform/train")
def uniform_train_start(store_id: int, camera_id: int | None = None,
                        db: Session = Depends(get_db),
                        _u=Depends(require_role("admin", "operator"))):
    """Kick off YOLOv8n-cls uniform training for a store. Requires >=30
    frames per class."""
    from sqlalchemy import func
    from app.models import TrainingSample
    rows = dict(
        db.query(TrainingSample.label, func.count(TrainingSample.id))
          .filter(TrainingSample.detector_type == "uniform",
                  TrainingSample.store_id == store_id)
          .group_by(TrainingSample.label).all()
    )
    # Validate the new 6-class spec; legacy 4-class counts still feed
    # the matching modern bucket via UNIFORM_LABEL_ALIASES at write
    # time, so the threshold check uses the canonical six.
    labels = ("full_compliant", "partial_compliant", "color_only",
              "non_compliant", "customer", "uncertain")
    short = {l: int(rows.get(l, 0)) for l in labels if int(rows.get(l, 0)) < 30}
    if short:
        raise HTTPException(
            400, "Need >=30 frames per class. Short: " +
            ", ".join(f"{k}={v}" for k, v in short.items()))
    from app.tasks.uniform_training import train_uniform_model
    train_uniform_model.delay(store_id, camera_id)
    return {"started": True, "store_id": store_id}


@router.get("/uniform/train/status")
def uniform_train_status(store_id: int,
                         _u=Depends(require_role("admin", "operator", "viewer"))):
    import json
    import redis
    from app.config import settings
    r = redis.from_url(settings.redis_url, decode_responses=True)
    raw = r.get(f"vg:uniform_train:status:{store_id}")
    if not raw:
        return {"state": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"state": "idle"}


@router.post("/uniform/deploy")
def uniform_deploy(model_id: int, camera_ids: list[int],
                   db: Session = Depends(get_db),
                   _u=Depends(require_role("admin", "operator"))):
    """Assign a trained uniform model to the given cameras so the
    UniformComplianceDetector uses it instead of the colour rules."""
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


# ====================================================================
# Image upload — uniform + shutter (Vivo P5)
# ====================================================================
# Operators upload phone photos / screenshots / WhatsApp images from
# their devices. Each upload is per-label (the form picks the label
# before the file lands so the operator can drag a folder of
# OK-uniform photos in one shot, then switch label and drop another
# folder).

MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB per spec
ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png"}
ALLOWED_EXT  = {".jpg", ".jpeg", ".png"}


def _save_uploaded(detector: str, label: str, camera_id: int | None,
                   store_id: int | None, user_id: int | None,
                   data: bytes, original_name: str,
                   root_fn) -> "TrainingSample":
    """Common write path for /uniform/upload + /shutter/upload."""
    from datetime import datetime, timezone
    from app.models import TrainingSample

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, f"unsupported file type — must be one of {sorted(ALLOWED_EXT)}")

    ts = datetime.now(timezone.utc)
    safe_name = "".join(c for c in Path(original_name).stem
                        if c.isalnum() or c in "-_") or "upload"
    fname = f"upload_{ts.strftime('%Y%m%d_%H%M%S_%f')}_{safe_name}{ext}"
    # `_shutter_sample_root` takes (label, camera_id); _uniform_sample_root
    # takes (label). Call uniformly via the closure caller passes in.
    path = root_fn(label) / fname
    path.write_bytes(data)
    sample = TrainingSample(
        detector_type=detector, label=label,
        camera_id=camera_id, store_id=store_id,
        frame_path=str(path), captured_at=ts,
        labeled_by=user_id,
        source="upload",
    )
    return sample


@router.post("/uniform/upload")
async def uniform_upload(label: str = Form(...),
                         camera_id: int | None = Form(None),
                         store_id: int | None = Form(None),
                         files: list[UploadFile] = File(...),
                         db: Session = Depends(get_db),
                         user=Depends(require_role("admin", "operator"))):
    """Upload one or more phone photos / screenshots for the uniform
    classifier, all tagged with the same `label`. Up to ~5 MB per file."""
    label = UNIFORM_LABEL_ALIASES.get((label or "").lower().strip(),
                                        (label or "").lower().strip())
    if label not in UNIFORM_LABELS:
        raise HTTPException(400, f"label must be one of {sorted(UNIFORM_LABELS)}")
    if not files:
        raise HTTPException(400, "no files in upload")

    out: list[dict] = []
    errors: list[dict] = []
    for f in files:
        try:
            data = await f.read()
            sample = _save_uploaded(
                "uniform", label, camera_id, store_id,
                getattr(user, "id", None), data, f.filename or "upload.jpg",
                _uniform_sample_root,
            )
            db.add(sample); db.flush()
            out.append({
                "sample_id": sample.id,
                "filename":  f.filename,
                "label":     sample.label,
                "preview_url": f"/api/training/uniform/samples/{sample.id}/file",
            })
        except HTTPException as e:
            errors.append({"filename": f.filename, "error": e.detail})
        except Exception as e:
            errors.append({"filename": f.filename, "error": str(e)})
    db.commit()
    return {"saved": len(out), "samples": out, "errors": errors}


# ====================================================================
# Chain-wide training (P5 overhaul)
# ====================================================================
# Pool approved samples from EVERY store and train one chain model per
# detector. The Celery task lives in app.tasks.chain_training and writes
# status to vg:chain_train:status:{detector_type}. Deploy fans the model
# out to every camera that has the matching zone tag.

CHAIN_DETECTOR_TYPES = {"shutter", "uniform"}


def _chain_camera_targets(db: Session, detector_type: str) -> list[int]:
    """Camera ids whose zones tag them as a target for this detector.
    Shutter → any zone with detection_type 'shutter'.
    Uniform → any zone with detection_type 'counter', 'staff' or
              'staff_zone'."""
    from app.models import Zone
    if detector_type == "shutter":
        tag_set = {"shutter"}
    elif detector_type == "uniform":
        tag_set = {"counter", "staff", "staff_zone"}
    else:
        return []
    cam_ids: set[int] = set()
    for z in db.query(Zone).all():
        if not z.camera_id:
            continue
        if tag_set & set(z.detection_types_json or []):
            cam_ids.add(z.camera_id)
    return sorted(cam_ids)


@router.get("/chain/dataset-summary")
def chain_dataset_summary(db: Session = Depends(get_db),
                          _u=Depends(require_role("admin", "operator", "viewer"))):
    """Per-detector breakdown of the chain training dataset.
    Shows what would be fed to the trainer if we kicked it off now:
    counts by label, contributing stores, ready-to-train flag."""
    from sqlalchemy import func, or_
    from app.models import Store, TrainingSample
    from app.tasks.chain_training import (
        MIN_PER_LABEL, UNIFORM_LABELS, SHUTTER_LABELS, UNIFORM_ALIASES,
    )

    store_names = {s.id: s.name for s in db.query(Store).all()}
    summary: dict[str, dict] = {}
    for detector_type, labels in (("shutter", SHUTTER_LABELS),
                                   ("uniform", UNIFORM_LABELS)):
        rows = (db.query(TrainingSample.label,
                         TrainingSample.store_id,
                         func.count(TrainingSample.id))
                  .filter(TrainingSample.detector_type == detector_type,
                          or_(TrainingSample.approved.is_(True),
                              TrainingSample.approved.is_(None)),
                          TrainingSample.source.in_(
                              ("capture", "upload", "camera_crop")))
                  .group_by(TrainingSample.label, TrainingSample.store_id)
                  .all())
        by_label: dict[str, int] = {l: 0 for l in labels}
        by_store: dict[int, dict] = {}
        stores_contributing: set[int] = set()
        total = 0
        for label, store_id, n in rows:
            lbl = (label or "").lower().strip()
            if detector_type == "uniform":
                lbl = UNIFORM_ALIASES.get(lbl, lbl)
            if lbl not in by_label:
                continue
            n = int(n)
            by_label[lbl] += n
            total += n
            if store_id is not None:
                stores_contributing.add(int(store_id))
                bucket = by_store.setdefault(
                    int(store_id),
                    {"store_id": int(store_id),
                     "store_name": store_names.get(int(store_id)),
                     "total": 0, "by_label": {l: 0 for l in labels}})
                bucket["total"] += n
                bucket["by_label"][lbl] += n
        ready = all(by_label[l] >= MIN_PER_LABEL for l in labels)
        short = {l: by_label[l] for l in labels if by_label[l] < MIN_PER_LABEL}
        summary[detector_type] = {
            "detector_type": detector_type,
            "labels": list(labels),
            "total_samples": total,
            "by_label": by_label,
            "by_store": sorted(by_store.values(),
                               key=lambda x: -x["total"]),
            "stores_contributing": sorted(stores_contributing),
            "min_per_label": MIN_PER_LABEL,
            "min_samples_met": ready,
            "ready_to_train": ready,
            "short_labels": short,
        }
    return summary


@router.post("/chain/train")
def chain_train_start(detector_type: str,
                      db: Session = Depends(get_db),
                      _u=Depends(require_role("admin", "operator"))):
    """Kick off the chain training task for one detector. The task
    pools every store's approved samples, applies quality filters and
    trains YOLOv8s-cls. Poll /training/chain/status/{detector_type}."""
    detector_type = (detector_type or "").lower().strip()
    if detector_type not in CHAIN_DETECTOR_TYPES:
        raise HTTPException(
            400, f"detector_type must be one of {sorted(CHAIN_DETECTOR_TYPES)}")
    from app.tasks.chain_training import train_chain_model
    async_result = train_chain_model.delay(detector_type)
    return {
        "started": True,
        "detector_type": detector_type,
        "task_id": getattr(async_result, "id", None),
    }


@router.get("/chain/status/{detector_type}")
def chain_train_status(detector_type: str,
                       _u=Depends(require_role("admin", "operator", "viewer"))):
    import json
    import redis
    from app.config import settings
    detector_type = (detector_type or "").lower().strip()
    if detector_type not in CHAIN_DETECTOR_TYPES:
        raise HTTPException(
            400, f"detector_type must be one of {sorted(CHAIN_DETECTOR_TYPES)}")
    r = redis.from_url(settings.redis_url, decode_responses=True)
    raw = r.get(f"vg:chain_train:status:{detector_type}")
    if not raw:
        return {"state": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"state": "idle"}


@router.get("/chain/models")
def chain_models(detector_type: str | None = None,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator", "viewer"))):
    """List chain models in the registry, newest first."""
    q = db.query(AIModel).filter(AIModel.is_chain_model == True)  # noqa: E712
    if detector_type:
        q = q.filter(AIModel.detector_type == detector_type.lower().strip())
    out = []
    for m in q.order_by(AIModel.id.desc()).all():
        out.append({
            "id": m.id, "name": m.name, "version": m.version,
            "detector_type": m.detector_type,
            "sample_count": m.sample_count,
            "trained_on_stores": m.trained_on_stores or [],
            "accuracy": m.map50,
            "precision": m.precision, "recall": m.recall,
            "deployed": bool(m.deployed),
            "weights_path": m.weights_path,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out


@router.post("/chain/deploy")
def chain_deploy(detector_type: str, model_id: int,
                 db: Session = Depends(get_db),
                 _u=Depends(require_role("admin", "operator"))):
    """Set ai_model_id on every camera whose zones target this
    detector. Marks the model deployed=True and undeploys the previous
    chain model for the same detector."""
    detector_type = (detector_type or "").lower().strip()
    if detector_type not in CHAIN_DETECTOR_TYPES:
        raise HTTPException(
            400, f"detector_type must be one of {sorted(CHAIN_DETECTOR_TYPES)}")
    model = db.get(AIModel, model_id)
    if not model:
        raise HTTPException(404, "model not found")
    if not (model.is_chain_model and model.detector_type == detector_type):
        raise HTTPException(
            400, "model is not a chain model for this detector_type")

    cam_ids = _chain_camera_targets(db, detector_type)
    cams = db.query(Camera).filter(Camera.id.in_(cam_ids)).all() if cam_ids else []
    for cam in cams:
        cam.ai_model_id = model_id

    # Mark prior chain models for this detector as undeployed.
    (db.query(AIModel)
       .filter(AIModel.is_chain_model == True,  # noqa: E712
               AIModel.detector_type == detector_type,
               AIModel.id != model_id)
       .update({AIModel.deployed: False}, synchronize_session=False))
    model.deployed = True
    db.commit()
    return {
        "model_id": model_id,
        "detector_type": detector_type,
        "deployed_to": [c.id for c in cams],
        "camera_count": len(cams),
    }


@router.post("/shutter/upload")
async def shutter_upload(label: str = Form(...),
                         camera_id: int | None = Form(None),
                         store_id: int | None = Form(None),
                         files: list[UploadFile] = File(...),
                         db: Session = Depends(get_db),
                         user=Depends(require_role("admin", "operator"))):
    """Upload one or more images for the shutter classifier
    (open / closed / partial)."""
    label = (label or "").lower().strip()
    if label not in SHUTTER_LABELS:
        raise HTTPException(400, f"label must be one of {sorted(SHUTTER_LABELS)}")
    if not files:
        raise HTTPException(400, "no files in upload")

    # _shutter_sample_root needs (label, camera_id); curry camera_id in.
    cam_for_root = camera_id or 0
    def _root(lbl):
        return _shutter_sample_root(lbl, cam_for_root)

    out: list[dict] = []
    errors: list[dict] = []
    for f in files:
        try:
            data = await f.read()
            sample = _save_uploaded(
                "shutter", label, camera_id, store_id,
                getattr(user, "id", None), data, f.filename or "upload.jpg",
                _root,
            )
            db.add(sample); db.flush()
            out.append({
                "sample_id": sample.id,
                "filename":  f.filename,
                "label":     sample.label,
                "preview_url": f"/api/training/shutter/samples/{sample.id}/file",
            })
        except HTTPException as e:
            errors.append({"filename": f.filename, "error": e.detail})
        except Exception as e:
            errors.append({"filename": f.filename, "error": str(e)})
    db.commit()
    return {"saved": len(out), "samples": out, "errors": errors}
