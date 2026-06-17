"""Chain-wide training pipeline (P5 overhaul).

ALL approved samples from EVERY store are pooled into one dataset and
trained as a single chain model. The result is auto-deployable to every
camera that has the matching detector (shutter zone or counter / staff
zone). Lives alongside the per-store trainers — those are kept for
operators that need a store-specific model, but the chain model is the
new default and the weekly auto-retrain (Monday 02:00 EAT, separate
commit) targets it.

Steps:
  1. Pull TrainingSample rows for `detector_type` where
     `approved` IS true or NULL  AND  source in (capture, upload,
     camera_crop). NULL=approved is treated as approved because the
     auto-harvester writes pending rows that operators rarely manage
     manually but are usually fine.
  2. Quality filters per frame, in order, dropping rows that fail:
       - Laplacian variance > 100 (sharp enough)
       - file-hash dedup (drop exact duplicates across stores)
       - sample-count cap per (label, store) to keep one big store from
         dominating
  3. 80/20 stratified split per label.
  4. YOLOv8s-cls training with the spec's augmentation params.
  5. Persist an AIModel row with is_chain_model=True, detector_type,
     trained_on_stores (list of store ids that contributed),
     sample_count (post-filter total).
  6. Status writes go to  vg:chain_train:status:{detector_type}.

The API layer (training.py) calls `train_chain_model.delay(detector_type)`
and polls the status key, then issues a separate deploy call once the
model row exists.
"""
from __future__ import annotations
import hashlib
import json
import logging
import random
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


SHUTTER_LABELS = ("open", "closed", "partial")
UNIFORM_LABELS = ("full_compliant", "partial_compliant", "color_only",
                  "non_compliant", "customer", "uncertain")

UNIFORM_ALIASES = {
    "uniform_ok":        "full_compliant",
    "no_lanyard":        "partial_compliant",
    "uniform_violation": "non_compliant",
    "civilian":          "customer",
}

# Per-(label, store) cap so one busy store can't dominate the dataset.
PER_STORE_PER_LABEL_CAP = 500
# Minimum acceptable samples per label across the whole chain.
MIN_PER_LABEL = 50
# Laplacian variance — anything below is rejected as too blurry.
BLUR_THRESHOLD = 100.0


def _labels_for(detector_type: str) -> tuple[str, ...]:
    if detector_type == "shutter":
        return SHUTTER_LABELS
    if detector_type == "uniform":
        return UNIFORM_LABELS
    raise ValueError(f"unsupported detector_type: {detector_type}")


def _canon_label(detector_type: str, label: str) -> str | None:
    """Normalise legacy aliases. Returns None if the label is not in
    the canonical set for this detector."""
    lbl = (label or "").lower().strip()
    if detector_type == "uniform":
        lbl = UNIFORM_ALIASES.get(lbl, lbl)
    return lbl if lbl in _labels_for(detector_type) else None


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _set_status(detector_type: str, payload: dict) -> None:
    try:
        r = _redis()
        key = f"vg:chain_train:status:{detector_type}"
        r.set(key, json.dumps(payload), ex=48 * 3600)
        r.publish(f"vg:pub:chain_train:{detector_type}", json.dumps(payload))
    except Exception:
        pass


def _is_sharp(path: Path) -> bool:
    """Laplacian variance > BLUR_THRESHOLD → sharp enough."""
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return False
        v = float(cv2.Laplacian(img, cv2.CV_64F).var())
        return v >= BLUR_THRESHOLD
    except Exception:
        # Fail open — if OpenCV chokes we'd rather keep the frame than
        # silently drop everyone's data.
        return True


def _file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha1()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _collect_samples(detector_type: str):
    """Pull approved (or NULL=pending) samples from the registry.
    Returns: { canonical_label: [ {path, store_id} ] }, contributing_stores set."""
    from sqlalchemy import or_
    from app.database import SessionLocal
    from app.models import TrainingSample

    grouped: dict[str, list[dict]] = defaultdict(list)
    contributing: set[int] = set()
    with SessionLocal() as db:
        q = (db.query(TrainingSample)
               .filter(TrainingSample.detector_type == detector_type,
                       or_(TrainingSample.approved.is_(True),
                           TrainingSample.approved.is_(None)),
                       TrainingSample.source.in_(
                           ("capture", "upload", "camera_crop"))))
        for s in q.all():
            lbl = _canon_label(detector_type, s.label)
            if not lbl:
                continue
            p = Path(s.frame_path)
            if not p.exists():
                continue
            grouped[lbl].append({"path": p, "store_id": s.store_id})
            if s.store_id is not None:
                contributing.add(int(s.store_id))
    return grouped, contributing


def _filter_and_balance(grouped: dict[str, list[dict]]) -> tuple[dict[str, list[Path]], dict]:
    """Apply blur + dedup + per-store cap. Returns ({label: [paths]}, stats)."""
    seen_hashes: set[str] = set()
    out: dict[str, list[Path]] = {}
    stats = {
        "input": {l: len(v) for l, v in grouped.items()},
        "dropped_blurry": 0, "dropped_dup": 0, "dropped_capped": 0,
        "kept_by_label": {}, "kept_by_store": defaultdict(int),
    }
    for label, rows in grouped.items():
        per_store: dict[int, int] = defaultdict(int)
        kept: list[Path] = []
        random.shuffle(rows)        # randomise dominance order
        for r in rows:
            sid = r["store_id"] or 0
            if per_store[sid] >= PER_STORE_PER_LABEL_CAP:
                stats["dropped_capped"] += 1
                continue
            if not _is_sharp(r["path"]):
                stats["dropped_blurry"] += 1
                continue
            h = _file_hash(r["path"])
            if h and h in seen_hashes:
                stats["dropped_dup"] += 1
                continue
            if h:
                seen_hashes.add(h)
            per_store[sid] += 1
            stats["kept_by_store"][sid] += 1
            kept.append(r["path"])
        out[label] = kept
        stats["kept_by_label"][label] = len(kept)
    stats["kept_by_store"] = dict(stats["kept_by_store"])
    return out, stats


def _build_dataset_dir(detector_type: str, kept: dict[str, list[Path]]) -> Path:
    root = (Path(settings.models_dir).parent
            / "chain_datasets" / detector_type)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    for split in ("train", "val"):
        for label in kept:
            (root / split / label).mkdir(parents=True, exist_ok=True)
    for label, paths in kept.items():
        random.shuffle(paths)
        cut = max(1, int(len(paths) * 0.8))
        for i, src in enumerate(paths):
            split = "train" if i < cut else "val"
            try:
                shutil.copy(src, root / split / label / f"{i}_{src.name}")
            except Exception:
                continue
    return root


def _train_yolo(root: Path, detector_type: str, ts: str) -> tuple[Path, dict]:
    """Train YOLOv8s-cls with the spec's augmentation params. Returns
    (best_weights_path, validation_report)."""
    from ultralytics import YOLO

    out_dir = Path(settings.models_dir) / f"chain_{detector_type}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    model = YOLO("yolov8s-cls.pt")

    def _on_epoch(trainer):
        try:
            epoch = int(getattr(trainer, "epoch", 0)) + 1
            _set_status(detector_type, {
                "state": "training",
                "message": f"Epoch {epoch}/50",
                "epoch": epoch, "total_epochs": 50,
            })
        except Exception:
            pass
    model.add_callback("on_train_epoch_end", _on_epoch)

    model.train(
        data=str(root), epochs=50, imgsz=224, batch=16,
        project=str(out_dir), name="run", exist_ok=True,
        device=("0" if settings.use_gpu else "cpu"),
        # Per-spec augmentation (P5 chain trainer):
        flipud=0.0, fliplr=0.5,
        hsv_h=0.1, hsv_s=0.3, hsv_v=0.3,
        degrees=10, translate=0.1, scale=0.3,
    )
    best = next(out_dir.rglob("best.pt"), None)
    if not best:
        raise RuntimeError("training finished but best.pt not found")
    return best, _validate(best, root, _labels_for(detector_type))


def _validate(weights: Path, dataset_root: Path, labels: tuple[str, ...]) -> dict:
    try:
        from ultralytics import YOLO
        import numpy as np
        m = YOLO(str(weights))
        metrics = m.val(data=str(dataset_root), split="val", verbose=False)
        acc = float(getattr(metrics, "top1", 0.0) or 0.0)
        report: dict = {"accuracy": round(acc, 4)}
        cm = getattr(metrics, "confusion_matrix", None)
        matrix = getattr(cm, "matrix", None) if cm is not None else None
        if matrix is not None:
            mat = np.array(matrix, dtype=float)
            n = len(labels)
            mat = mat[:n, :n]
            per_class, precisions, recalls = {}, [], []
            for i, label in enumerate(labels):
                tp = mat[i, i]; col = mat[:, i].sum(); row = mat[i, :].sum()
                prec = (tp / col) if col else 0.0
                rec  = (tp / row) if row else 0.0
                per_class[label] = {"precision": round(prec, 3),
                                    "recall":    round(rec, 3)}
                precisions.append(prec); recalls.append(rec)
            report["per_class"]       = per_class
            report["precision_macro"] = round(sum(precisions) / len(precisions), 3)
            report["recall_macro"]    = round(sum(recalls)    / len(recalls), 3)
        report["recommendation"] = (
            "Deploy chain-wide ✅" if report.get("accuracy", 0) >= 0.85
            else "Collect more frames and retrain before deploying ⚠️")
        return report
    except Exception as e:
        log.warning("chain validation failed: %s", e)
        return {"accuracy": None, "recommendation": "Validation unavailable"}


def _notify_chain(detector_type: str, model_id: int, report: dict,
                  sample_count: int, store_count: int) -> None:
    try:
        acc = report.get("accuracy")
        lines = [
            f"Chain {detector_type} model training complete.",
            f"Model id: {model_id}",
            f"Samples: {sample_count} from {store_count} stores",
            (f"Accuracy: {round(acc * 100, 1)}%"
             if acc is not None else "Accuracy: n/a"),
            report.get("recommendation", ""),
        ]
        from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
        to = _format_whatsapp_recipient(getattr(settings, "dashboard_alert_to", ""))
        if to:
            _send_whatsapp([to], "\n".join(lines))
    except Exception:
        pass


@celery_app.task(name="training.train_chain_model", bind=True, ignore_result=True)
def train_chain_model(self, detector_type: str) -> None:
    """Pool every store's approved samples and train one chain model
    for `detector_type` ('shutter' or 'uniform')."""
    detector_type = (detector_type or "").lower().strip()
    try:
        labels = _labels_for(detector_type)
    except ValueError as e:
        _set_status(detector_type or "unknown",
                    {"state": "failed", "message": str(e)})
        return

    _set_status(detector_type, {
        "state": "preparing", "message": "Pooling samples from every store…",
    })
    grouped, contributing = _collect_samples(detector_type)
    if not grouped:
        _set_status(detector_type, {
            "state": "failed",
            "message": "No approved samples found in the chain dataset.",
        })
        return

    _set_status(detector_type, {
        "state": "filtering",
        "message": "Quality filters: blur, dedup, per-store balance…",
    })
    kept, stats = _filter_and_balance(grouped)

    short = {l: stats["kept_by_label"].get(l, 0) for l in labels
             if stats["kept_by_label"].get(l, 0) < MIN_PER_LABEL}
    if short:
        msg = ("Need ≥%d frames per label after filtering. Short: %s"
               % (MIN_PER_LABEL,
                  ", ".join(f"{k}={v}" for k, v in short.items())))
        _set_status(detector_type, {"state": "failed", "message": msg,
                                    "stats": stats})
        log.warning("chain training %s aborted: %s", detector_type, msg)
        return

    # Imbalance warning (informational; we still train).
    counts = [stats["kept_by_label"][l] for l in labels]
    imbalance_warning = None
    if min(counts) and max(counts) / min(counts) > 5:
        imbalance_warning = (
            "Heavy class imbalance — strongest class has %.1fx more samples "
            "than the weakest. Consider collecting more of the rare classes."
            % (max(counts) / min(counts)))

    _set_status(detector_type, {
        "state": "preparing",
        "message": "Building train/val folders (80/20 stratified)…",
        "stats": stats,
    })
    root = _build_dataset_dir(detector_type, kept)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _set_status(detector_type, {
        "state": "training", "message": "Training YOLOv8s-cls…",
        "epoch": 0, "total_epochs": 50, "stats": stats,
    })
    try:
        best, report = _train_yolo(root, detector_type, ts)
        if imbalance_warning:
            report["warning"] = imbalance_warning

        from app.database import SessionLocal
        from app.models import AIModel
        sample_count = sum(stats["kept_by_label"].values())
        with SessionLocal() as db:
            ai_model = AIModel(
                name=f"Chain — {detector_type}",
                version=f"chain-{ts}",
                base_model="yolov8s-cls.pt",
                classes_json=list(labels),
                weights_path=str(best),
                export_format="pt",
                map50=report.get("accuracy"),
                precision=report.get("precision_macro"),
                recall=report.get("recall_macro"),
                is_chain_model=True,
                detector_type=detector_type,
                trained_on_stores=sorted(contributing),
                sample_count=sample_count,
            )
            db.add(ai_model); db.commit(); db.refresh(ai_model)
            model_id = ai_model.id

        result = {
            "state": "done", "message": "Training complete",
            "model_id": model_id, "weights": str(best),
            "report": report, "stats": stats,
            "trained_on_stores": sorted(contributing),
            "sample_count": sample_count,
        }
        _set_status(detector_type, result)
        _notify_chain(detector_type, model_id, report,
                      sample_count, len(contributing))
        log.info("chain training %s done: model=%s acc=%s samples=%s stores=%s",
                 detector_type, model_id, report.get("accuracy"),
                 sample_count, len(contributing))
    except Exception as e:
        log.exception("chain training %s failed: %s", detector_type, e)
        _set_status(detector_type, {"state": "failed", "message": str(e)})
        raise


# ----------------------------------------------------------------------
# Weekly auto-retrain dispatcher
# ----------------------------------------------------------------------
# Fires every 5 minutes from celery beat. On Monday 02:00–02:05 chain
# time (Africa/Nairobi) it kicks off training for every detector whose
# dataset has the minimum samples + has grown since the last training
# run, then arms an iso-week marker in Redis so duplicates are ignored.

AUTO_RETRAIN_HOUR = 2          # 02:00 chain-time Monday
AUTO_RETRAIN_WEEKDAY = 0       # Monday

def _chain_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.app_timezone)
    except Exception:
        from datetime import timezone as _tz
        return _tz.utc


def _has_grown_since_last_train(detector_type: str) -> bool:
    """True if the approved-sample count for this detector exceeds the
    sample_count baked into the latest chain model for the same
    detector. If no chain model exists yet, treat as grown."""
    from sqlalchemy import or_, func
    from app.database import SessionLocal
    from app.models import AIModel, TrainingSample

    with SessionLocal() as db:
        latest = (db.query(AIModel)
                    .filter(AIModel.is_chain_model == True,            # noqa: E712
                            AIModel.detector_type == detector_type)
                    .order_by(AIModel.id.desc()).first())
        current = (db.query(func.count(TrainingSample.id))
                     .filter(TrainingSample.detector_type == detector_type,
                             or_(TrainingSample.approved.is_(True),
                                 TrainingSample.approved.is_(None)),
                             TrainingSample.source.in_(
                                 ("capture", "upload", "camera_crop")))
                     .scalar() or 0)
        if latest is None or latest.sample_count is None:
            return current > 0
        return int(current) > int(latest.sample_count)


@celery_app.task(name="training.chain_retrain_due", ignore_result=True)
def chain_retrain_due() -> None:
    """5-min beat tick. Auto-fires the chain trainer when:
      - sample count for the detector has grown by ≥
        settings.retrain_min_feedback since the last chain model, OR
      - calendar days since the last chain model ≥
        settings.retrain_max_days  (weekly fallback)
    Per-detector daily dedup. The legacy Monday-02:00 gate was too
    strict — stores accumulating 700+ confirmed alerts could go
    weeks without a retrain.
    """
    from app.config import settings
    from sqlalchemy import func, or_
    from app.database import SessionLocal
    from app.models import AIModel, TrainingSample

    min_feedback = int(getattr(settings, "retrain_min_feedback", 20))
    max_days     = int(getattr(settings, "retrain_max_days", 7))

    now_local = datetime.now(timezone.utc).astimezone(_chain_tz())
    day_iso = now_local.date().isoformat()
    try:
        r = _redis()
    except Exception:
        return

    for detector_type in ("uniform", "shutter"):
        # Daily dedup — once we've decided "fire" or "skip" for the
        # day, don't reconsider until tomorrow. The marker is the
        # decision day, not the iso-week, so the threshold can catch
        # bursts mid-week.
        key = f"vg:chain_train:auto:{detector_type}:day"
        try:
            if r.get(key) == day_iso:
                continue

            with SessionLocal() as db:
                latest = (db.query(AIModel)
                            .filter(AIModel.is_chain_model == True,        # noqa: E712
                                    AIModel.detector_type == detector_type)
                            .order_by(AIModel.id.desc()).first())
                current = int((db.query(func.count(TrainingSample.id))
                                 .filter(TrainingSample.detector_type == detector_type,
                                         or_(TrainingSample.approved.is_(True),
                                             TrainingSample.approved.is_(None)),
                                         TrainingSample.source.in_(
                                             ("capture", "upload", "camera_crop")))
                                 .scalar()) or 0)

            if latest is None:
                # No chain model yet — fire on any approved data.
                enough_samples, overdue = (current > 0), True
                delta = current
                days_old = float("inf")
            else:
                delta = current - int(latest.sample_count or 0)
                enough_samples = delta >= min_feedback
                created = latest.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_old = ((datetime.now(timezone.utc) - created).total_seconds() / 86400.0
                            if created else float("inf"))
                overdue = days_old >= max_days

            if not (enough_samples or overdue):
                # Set marker so we don't reconsider for the rest of
                # today; the next chance is tomorrow's first tick.
                r.set(key, day_iso, ex=2 * 24 * 3600)
                continue

            train_chain_model.delay(detector_type)
            r.set(key, day_iso, ex=2 * 24 * 3600)
            trigger = "sample_threshold" if enough_samples else "weekly_fallback"
            _notify_auto_retrain_started(detector_type)
            log.info("chain auto-retrain queued for %s — trigger=%s "
                     "delta_samples=%d days_since_last=%.1f",
                     detector_type, trigger, delta, days_old)
        except Exception as e:
            log.warning("auto-retrain dispatch failed for %s: %s",
                        detector_type, e)


def _notify_auto_retrain_started(detector_type: str) -> None:
    try:
        from app.tasks.briefings import _send_whatsapp, _format_whatsapp_recipient
        to = _format_whatsapp_recipient(getattr(settings, "dashboard_alert_to", ""))
        if to:
            _send_whatsapp([to], (
                f"🔁 Weekly chain retrain started — {detector_type}.\n"
                "Pooled samples from every store. You'll get another "
                "WhatsApp when training completes with accuracy stats."
            ))
    except Exception:
        pass
