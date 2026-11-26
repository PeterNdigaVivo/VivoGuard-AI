"""Model deployment safety: compatibility, canary rollout, manual promotion.

Live inference reads exactly one thing: ``Camera.ai_model_id`` (NULL means
the base COCO model, ``settings.default_model``). This module is the only
code that should change it, and it only does so in three explicit,
operator-initiated steps:

1. ``start_canary``  — a compatible model goes to a few selected cameras.
2. ``evaluate``      — canary metrics are computed from reviewed verdicts
                       on those cameras, against the same cameras' baseline
                       before the canary started, and recorded.
3. ``promote``       — a person promotes to an explicit list of cameras,
                       only after an evaluation that passed.
   ``rollback``      — puts every touched camera back exactly as it was.

There is no automatic or fleet-wide deployment anywhere: training ends
with the candidate *staged*, never deployed.

Compatibility is strict because one model drives the whole person
pipeline on a camera: a classifier, or a detector without a ``person``
class, would blind every person-based detector on that camera. The
inference worker enforces the same rule at load time
(``person_compatible``) and falls back to the base model.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIModel, Alert, AlertReviewDecision, Camera, DetectionEvent,
)
from app.models.model_rollout import ModelEvaluation, ModelRollout

log = logging.getLogger(__name__)

STAGES = ("canary", "promoted", "rolled_back")


class RolloutError(ValueError):
    pass


# ------------------------------------------------------------ compatibility

def is_classifier(m: AIModel | None) -> bool:
    if m is None:
        return False
    text = f"{m.base_model or ''} {m.weights_path or ''}".lower()
    return "-cls" in text or "_cls" in text


def person_compatible(m: AIModel | None) -> tuple[bool, str]:
    """Can this model drive a camera's general (person) pipeline?"""
    if m is None:
        return False, "model not found"
    if is_classifier(m):
        return False, "classification model; cannot replace the camera detector"
    classes = [str(c).lower() for c in (m.classes_json or [])]
    if "person" not in classes:
        return False, "model has no compatible 'person' class"
    if not m.weights_path or not os.path.exists(m.weights_path):
        return False, "weights file missing on this server"
    return True, "ok"


# ------------------------------------------------------------------ canary

def _active_rollouts_for(db: Session, camera_ids: list[int]) -> list[ModelRollout]:
    out = []
    for r in db.query(ModelRollout).filter(ModelRollout.stage == "canary").all():
        if set(r.camera_ids_json or []) & set(camera_ids):
            out.append(r)
    return out


def start_canary(db: Session, model_id: int, camera_ids: list[int], *,
                 user_id: int, notes: str | None = None) -> ModelRollout:
    m = db.get(AIModel, model_id)
    ok, why = person_compatible(m)
    if not ok:
        raise RolloutError(why)
    ids = sorted({int(c) for c in camera_ids})
    max_n = int(getattr(settings, "model_canary_max_cameras", 3))
    if not ids:
        raise RolloutError("select at least one canary camera")
    if len(ids) > max_n:
        raise RolloutError(f"a canary may use at most {max_n} cameras")
    cams = db.query(Camera).filter(Camera.id.in_(ids),
                                   Camera.is_deleted.is_(False)).all()
    if len(cams) != len(ids):
        raise RolloutError("unknown or deleted camera in the canary set")
    if _active_rollouts_for(db, ids):
        raise RolloutError("a camera in this set is already in a canary")
    previous = {str(c.id): c.ai_model_id for c in cams}
    for c in cams:
        c.ai_model_id = m.id
    r = ModelRollout(model_id=m.id, stage="canary", camera_ids_json=ids,
                     previous_assignment_json=previous, started_by=user_id,
                     notes=notes)
    db.add(r)
    db.flush()
    log.info("canary: model %s on cameras %s by user %s", m.id, ids, user_id)
    return r


# -------------------------------------------------------------- evaluation

def _verdict_counts(db: Session, camera_ids: list[int], start: datetime,
                    end: datetime, model_id: int | None) -> dict:
    """TP / FP from each alert's latest review decision in the window."""
    latest = (db.query(AlertReviewDecision.alert_id,
                       func.max(AlertReviewDecision.id).label("mid"))
                .group_by(AlertReviewDecision.alert_id).subquery())
    q = (db.query(DetectionEvent.detection_type, AlertReviewDecision.verdict,
                  func.count(Alert.id))
           .join(Alert, Alert.event_id == DetectionEvent.id)
           .join(latest, latest.c.alert_id == Alert.id)
           .join(AlertReviewDecision, AlertReviewDecision.id == latest.c.mid)
           .filter(DetectionEvent.camera_id.in_(camera_ids),
                   DetectionEvent.timestamp >= start,
                   DetectionEvent.timestamp < end))
    q = (q.filter(DetectionEvent.model_id == model_id) if model_id is not None
         else q.filter(DetectionEvent.model_id.is_(None)))
    per: dict[str, dict] = {}
    for dtype, verdict, n in q.group_by(DetectionEvent.detection_type,
                                       AlertReviewDecision.verdict).all():
        row = per.setdefault(dtype, {"tp": 0, "fp": 0})
        if verdict == "confirmed":
            row["tp"] += int(n)
        elif verdict == "dismissed":
            row["fp"] += int(n)
    tp = sum(r["tp"] for r in per.values())
    fp = sum(r["fp"] for r in per.values())
    return {"tp": tp, "fp": fp, "reviewed": tp + fp,
            "precision": (tp / (tp + fp)) if tp + fp else None,
            "false_alert_rate": (fp / (tp + fp)) if tp + fp else None,
            "per_detector": {
                k: {**v, "precision": (v["tp"] / (v["tp"] + v["fp"]))
                    if v["tp"] + v["fp"] else None}
                for k, v in sorted(per.items())}}


def evaluate(db: Session, rollout: ModelRollout,
             now: datetime | None = None) -> ModelEvaluation:
    """Canary vs the same cameras' baseline over an equal earlier window."""
    if rollout.stage != "canary":
        raise RolloutError("only a running canary can be evaluated")
    now = now or datetime.now(timezone.utc)
    start = rollout.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    window = max(now - start, timedelta(hours=1))
    cams = list(rollout.camera_ids_json or [])
    canary = _verdict_counts(db, cams, start, now, rollout.model_id)
    prev_ids = {v for v in (rollout.previous_assignment_json or {}).values()}
    prev_model = next(iter(prev_ids)) if len(prev_ids) == 1 else None
    baseline = _verdict_counts(db, cams, start - window, start, prev_model)

    min_reviewed = int(getattr(settings, "model_canary_min_reviewed", 30))
    tolerance = float(getattr(settings, "model_canary_precision_tolerance", 0.0))
    reasons = []
    if canary["reviewed"] < min_reviewed:
        reasons.append(f"{canary['reviewed']} reviewed canary alerts, need {min_reviewed}")
    if (canary["precision"] is not None and baseline["precision"] is not None
            and canary["precision"] + tolerance < baseline["precision"]):
        reasons.append(f"canary precision {canary['precision']:.2f} below "
                       f"baseline {baseline['precision']:.2f}")
    ev = ModelEvaluation(
        model_id=rollout.model_id, rollout_id=rollout.id, split="canary",
        precision=canary["precision"], recall=None,
        false_positive_rate=canary["false_alert_rate"],
        sample_count=canary["reviewed"],
        metrics_json={"canary": canary, "baseline": baseline,
                      "baseline_model_id": prev_model,
                      "window_hours": round(window.total_seconds() / 3600, 1)},
        passed=not reasons, reasons_json=reasons,
    )
    db.add(ev)
    rollout.evaluated_at = now
    db.flush()
    return ev


def record_training_evaluation(db: Session, model: AIModel, metrics: dict,
                               split: str = "validation") -> ModelEvaluation:
    """Store a trainer / held-out evaluation for a candidate model.

    ``metrics`` may carry precision, recall, map50, map50_95,
    false_positive_rate (image-level FPR on background images) and
    per_class. Missing values stay NULL rather than being guessed.
    """
    ev = ModelEvaluation(
        model_id=model.id, split=split,
        precision=metrics.get("precision"), recall=metrics.get("recall"),
        false_positive_rate=metrics.get("false_positive_rate"),
        sample_count=metrics.get("sample_count"),
        metrics_json=metrics, passed=None, reasons_json=[],
    )
    db.add(ev)
    db.flush()
    return ev


# ----------------------------------------------------------- promote / back

def promote(db: Session, rollout: ModelRollout, camera_ids: list[int], *,
            user_id: int) -> ModelRollout:
    """Manual promotion to an explicit camera list after a passed canary."""
    if rollout.stage != "canary":
        raise RolloutError(f"rollout is {rollout.stage}, not canary")
    last = (db.query(ModelEvaluation)
              .filter(ModelEvaluation.rollout_id == rollout.id)
              .order_by(ModelEvaluation.id.desc()).first())
    if last is None:
        raise RolloutError("evaluate the canary before promoting")
    if not last.passed:
        raise RolloutError("canary evaluation did not pass: "
                           + "; ".join(last.reasons_json or []))
    m = db.get(AIModel, rollout.model_id)
    ok, why = person_compatible(m)
    if not ok:
        raise RolloutError(why)
    ids = sorted({int(c) for c in camera_ids} | set(rollout.camera_ids_json or []))
    cams = db.query(Camera).filter(Camera.id.in_(ids),
                                   Camera.is_deleted.is_(False)).all()
    if len(cams) != len(ids):
        raise RolloutError("unknown or deleted camera in the promotion set")
    previous = dict(rollout.previous_assignment_json or {})
    for c in cams:
        previous.setdefault(str(c.id), c.ai_model_id)
        c.ai_model_id = m.id
    rollout.previous_assignment_json = previous
    rollout.camera_ids_json = ids
    rollout.stage = "promoted"
    rollout.promoted_by = user_id
    rollout.promoted_at = datetime.now(timezone.utc)
    m.deployed = True
    db.flush()
    log.info("promote: model %s to cameras %s by user %s", m.id, ids, user_id)
    return rollout


def rollback(db: Session, rollout: ModelRollout, *, user_id: int) -> ModelRollout:
    """Restore every camera this rollout touched to its prior model."""
    if rollout.stage == "rolled_back":
        raise RolloutError("already rolled back")
    for cam_id, prev in (rollout.previous_assignment_json or {}).items():
        cam = db.get(Camera, int(cam_id))
        if cam is not None and cam.ai_model_id == rollout.model_id:
            cam.ai_model_id = prev
    rollout.stage = "rolled_back"
    rollout.rolled_back_by = user_id
    rollout.rolled_back_at = datetime.now(timezone.utc)
    still_used = (db.query(Camera.id)
                    .filter(Camera.ai_model_id == rollout.model_id).first())
    if still_used is None:
        m = db.get(AIModel, rollout.model_id)
        if m is not None:
            m.deployed = False
    db.flush()
    return rollout


def fleet_model_view(db: Session) -> dict:
    """What each camera actually runs: base model, assigned model, canary."""
    base = getattr(settings, "default_model", "yolov8n.pt")
    models = {m.id: m for m in db.query(AIModel).all()}
    canary_cams: dict[int, int] = {}
    for r in db.query(ModelRollout).filter(ModelRollout.stage == "canary"):
        for c in r.camera_ids_json or []:
            canary_cams[int(c)] = r.id
    cams = []
    for c in (db.query(Camera).filter(Camera.is_deleted.is_(False))
                .order_by(Camera.store_id, Camera.name)):
        m = models.get(c.ai_model_id) if c.ai_model_id else None
        ok, why = person_compatible(m) if m else (True, "base")
        cams.append({
            "camera_id": c.id, "camera_name": c.name, "store_id": c.store_id,
            "assigned_model_id": c.ai_model_id,
            "assigned_model": (f"{m.name} {m.version}" if m else None),
            # What the worker will really load (incompatible -> base).
            "effective_model": (f"{m.name} {m.version}" if m and ok else base),
            "fallback_reason": None if ok else why,
            "canary_rollout_id": canary_cams.get(c.id),
        })
    return {"base_model": base, "cameras": cams}
