"""Direct label promotion: the operator's mark IS the training label.

One click on the Alerts page (True / False / Unclear / Bad image) becomes
the alert's single training row. Whether that row may train is decided
here, at the moment of marking, by three safeguards that replace the old
"approve every label" step:

1. Trust / agreement gate. A trainable label is auto-promoted when the
   operator is a trusted labeller, OR their mark agrees with the model.
   The model's view is the AI second opinion (alert.ai_verdict) whenever
   it has a clear verdict, otherwise the detector's own confidence:
       True  agrees at confidence >= LABEL_AGREE_TRUE_MIN_CONFIDENCE
       False agrees at confidence <  LABEL_AGREE_FALSE_MAX_CONFIDENCE
   In between, or opposite: a disagreement -> held for review.
2. Burst guard. LABEL_BURST_COUNT same-way marks by one person within
   LABEL_BURST_WINDOW_MINUTES are held, including the earlier marks of
   the same burst, so clearing a queue cannot poison a model.
3. Audit sample. LABEL_AUDIT_SAMPLE_RATE of auto-promoted labels are
   also flagged for a spot-check in Training Review (they still train).

Everything else is decided by the mark itself (training_evidence.
classify_feedback): Unclear / Bad image / policy errors / labels with no
learnable class are excluded; a plain False on a person-based alert
without the "No person there" choice is held as ambiguous.

With TRAINING_DIRECT_PROMOTION_ENABLED off, every trainable label waits
in Training Review exactly as before (path ``pending_manual``).

The gate runs inside the training-sample task; there is no scheduled job.
Every decision is written on the row (promotion_path, gate_json,
labelled_by_user_id, labelled_at) and to governance_audit_log.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AlertReviewDecision, TrainingImage
from app.services import training_evidence as te

AUTO_PATHS = ("auto_agreement", "auto_trusted")
HELD_PATHS = ("held_disagreement", "held_burst", "held_ambiguous", "pending_manual")

_EXCLUDED_PATH_FOR_REASON = {
    "excluded_unclear": "excluded_unclear",
    "excluded_bad_image": "excluded_bad_image",
    "behaviour_rule_not_an_object_class": "excluded_no_class",
    "no_object_class": "excluded_no_class",
}


@dataclass
class GateResult:
    path: str
    review_state: str
    eligible: bool
    audit_sample: bool = False
    detail: dict = field(default_factory=dict)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def model_signal(ai_verdict: str | None, ai_confidence: float | None,
                 detector_confidence: float | None) -> dict:
    """What the model thinks: 'true', 'false' or 'uncertain', and why."""
    if ai_verdict in ("true_alert", "false_alert"):
        return {"source": "ai_second_opinion",
                "says": "true" if ai_verdict == "true_alert" else "false",
                "confidence": ai_confidence}
    if detector_confidence is None:
        return {"source": "none", "says": "uncertain", "confidence": None}
    hi = float(getattr(settings, "label_agree_true_min_confidence", 0.60))
    lo = float(getattr(settings, "label_agree_false_max_confidence", 0.45))
    c = float(detector_confidence)
    says = "true" if c >= hi else "false" if c < lo else "uncertain"
    return {"source": "detector_confidence", "says": says, "confidence": c,
            "thresholds": {"true_min": hi, "false_max": lo}}


def same_way_marks(db: Session, user_id: int | None, verdict: str,
                   at: datetime) -> int:
    """Marks of the same verdict by this person within the burst window
    ending at ``at`` (this mark included)."""
    if user_id is None:
        return 0
    minutes = int(getattr(settings, "label_burst_window_minutes", 10))
    start = at - timedelta(minutes=max(1, minutes))
    return int(db.query(func.count(AlertReviewDecision.id))
                 .filter(AlertReviewDecision.reviewer_id == user_id,
                         AlertReviewDecision.verdict == verdict,
                         AlertReviewDecision.created_at >= start,
                         AlertReviewDecision.created_at <= at)
                 .scalar() or 0)


def decide(db: Session, fc: te.FeedbackClass, *, user, verdict: str | None,
           marked_at: datetime, ai_verdict: str | None,
           ai_confidence: float | None, detector_confidence: float | None,
           rng=random.random) -> GateResult:
    """The promotion decision for one label (no writes)."""
    if not fc.gated:
        if fc.review_state == te.PENDING:      # person False without choice
            return GateResult("held_ambiguous", te.PENDING, False,
                              detail={"reason": fc.reason})
        path = _EXCLUDED_PATH_FOR_REASON.get(fc.reason)
        if path is None and fc.reason.startswith("policy_error"):
            path = "policy_error"
        return GateResult(path or "excluded_no_class", fc.review_state, False,
                          detail={"reason": fc.reason})

    if not bool(getattr(settings, "training_direct_promotion_enabled", False)):
        return GateResult("pending_manual", te.PENDING, False,
                          detail={"reason": "direct_promotion_disabled"})

    detail: dict = {"reason": fc.reason}
    burst_n = int(getattr(settings, "label_burst_count", 15))
    if verdict is not None:
        n = same_way_marks(db, getattr(user, "id", None), verdict, marked_at)
        detail["same_way_marks_in_window"] = n
        if n >= max(2, burst_n):
            detail["burst"] = {"count": n, "limit": burst_n,
                               "window_minutes": int(getattr(
                                   settings, "label_burst_window_minutes", 10))}
            return GateResult("held_burst", te.PENDING, False, detail=detail)

    if bool(getattr(user, "trusted_labeller", False)):
        path = "auto_trusted"
    else:
        signal = model_signal(ai_verdict, ai_confidence, detector_confidence)
        detail["model"] = signal
        wanted = "true" if fc.kind == te.POSITIVE else "false"
        if signal["says"] != wanted:
            return GateResult("held_disagreement", te.PENDING, False, detail=detail)
        path = "auto_agreement"
    rate = float(getattr(settings, "label_audit_sample_rate", 0.05))
    return GateResult(path, te.APPROVED, True, audit_sample=rng() < rate,
                      detail=detail)


def hold_earlier_burst(db: Session, *, user_id: int | None, kind: str,
                       at: datetime, exclude_id: int | None = None) -> int:
    """When a burst is detected, the marks that led up to it are held too."""
    if user_id is None:
        return 0
    minutes = int(getattr(settings, "label_burst_window_minutes", 10))
    rows = (db.query(TrainingImage)
              .filter(TrainingImage.labelled_by_user_id == user_id,
                      TrainingImage.example_kind == kind,
                      TrainingImage.promotion_path.in_(AUTO_PATHS),
                      TrainingImage.labelled_at >= at - timedelta(minutes=minutes))
              .all())
    n = 0
    for img in rows:
        if exclude_id is not None and img.id == exclude_id:
            continue
        img.review_state = te.PENDING
        img.eligible_for_training = False
        img.promotion_path = "held_burst"
        img.gate_json = {**(img.gate_json or {}), "burst_retro_held_at": at.isoformat()}
        n += 1
    return n


def apply(img: TrainingImage, result: GateResult, *, user_id: int | None,
          marked_at: datetime, extra: dict | None = None) -> None:
    img.review_state = result.review_state
    img.eligible_for_training = result.eligible
    img.promotion_path = result.path
    img.audit_sample = bool(result.audit_sample)
    img.labelled_by_user_id = user_id
    img.labelled_at = marked_at
    img.gate_json = {**result.detail, **(extra or {}),
                     "decided_at": datetime.now(timezone.utc).isoformat()}


def audit(db: Session, img: TrainingImage, *, user, alert_id: int,
          mark: str) -> None:
    """One governance_audit_log row per label decision."""
    from app.models import GovernanceAuditLog
    db.add(GovernanceAuditLog(
        actor_user_id=getattr(user, "id", None),
        actor_email=getattr(user, "email", None),
        action=f"training_label.{img.promotion_path}",
        entity_type="training_image",
        entity_id=str(img.id),
        details={"alert_id": alert_id, "mark": mark,
                 "example_kind": img.example_kind,
                 "target_class": img.target_class,
                 "eligible": bool(img.eligible_for_training),
                 "audit_sample": bool(img.audit_sample),
                 "gate": img.gate_json},
    ))


def latest_decision(db: Session, alert_id: int) -> AlertReviewDecision | None:
    return (db.query(AlertReviewDecision)
              .filter(AlertReviewDecision.alert_id == alert_id)
              .order_by(AlertReviewDecision.id.desc()).first())


def marked_at_for(decision: AlertReviewDecision | None) -> datetime:
    return _aware(getattr(decision, "created_at", None)) or datetime.now(timezone.utc)
