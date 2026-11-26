"""Training evidence: what an operator verdict is allowed to teach a model.

An operator's True/False click on an alert is *evidence*, not a label. It
lands here in ``pending_review`` and is never trained on until someone
reviews it in the evidence queue. This replaces the old behaviour where a
click was stamped ``approved`` and could queue a retrain on the spot.

Review states (TrainingImage.review_state)::

    pending_review  waiting for a reviewer (never trained)
    approved        reviewed and trainable (only state the trainer reads)
    rejected        reviewer said the evidence is wrong
    excluded        kept for audit/analysis, never trainable

Example kinds (TrainingImage.example_kind)::

    positive      the target object is present and boxed
    negative      background: the target object is NOT in the frame
    uncertain     cannot tell yet (e.g. dismissed without a reason)
    policy_error  the detector saw something real but the business rule
                  was wrong (staff member, normal activity). Training this
                  as "nothing here" would teach the model that real
                  people are not people, so it is never trainable.

Only object classes a detector can actually learn get a ``target_class``.
Behaviour rules (intrusion, loitering, staff_zone, ...) are not object
classes; their confirmed frames are kept as evaluation evidence, not
trained into a class named after the rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Annotation, TrainingImage

PENDING = "pending_review"
APPROVED = "approved"
REJECTED = "rejected"
EXCLUDED = "excluded"
REVIEW_STATES = (PENDING, APPROVED, REJECTED, EXCLUDED)
# Older code paths write "pending"; treated as pending_review.
PENDING_STATES = (PENDING, "pending")

POSITIVE = "positive"
NEGATIVE = "negative"
UNCERTAIN = "uncertain"
POLICY_ERROR = "policy_error"
EXAMPLE_KINDS = (POSITIVE, NEGATIVE, UNCERTAIN, POLICY_ERROR)

# detection_type -> the object class a detector model can learn.
OBJECT_CLASS_FOR_TYPE: dict[str, str] = {
    "person": "person", "fire": "fire", "smoke": "smoke", "weapon": "weapon",
    "weapon_brandished": "weapon", "vehicle": "vehicle", "animal": "animal",
    "abandoned_object": "abandoned_object",
}
# Behaviour rules whose false alarms CAN say something about the person
# class: "not a person" on an intrusion alert means no person in frame.
PERSON_FAMILY_TYPES = frozenset({
    "person", "intrusion", "trespass", "staff_zone", "loitering",
    "live_activity", "tailgating", "stockroom_access",
})

DISMISS_REASON_KIND = {
    "not_a_person": NEGATIVE,
    "camera_problem": UNCERTAIN,     # image unusable: excluded below
    "staff_member": POLICY_ERROR,
    "normal_activity": POLICY_ERROR,
    "something_else": UNCERTAIN,
}


@dataclass(frozen=True)
class FeedbackClass:
    kind: str
    target_class: str | None
    review_state: str
    reason: str


def classify_feedback(verdict: str, detection_type: str,
                      dismiss_reason: str | None = None) -> FeedbackClass:
    """Map one operator verdict to (kind, target class, initial state)."""
    if verdict in ("correct", "confirm", "true"):
        cls = OBJECT_CLASS_FOR_TYPE.get(detection_type)
        if cls is None:
            return FeedbackClass(POSITIVE, None, EXCLUDED,
                                 "behaviour_rule_not_an_object_class")
        return FeedbackClass(POSITIVE, cls, PENDING, "confirmed_by_operator")

    kind = DISMISS_REASON_KIND.get(dismiss_reason or "", UNCERTAIN)
    if dismiss_reason == "camera_problem":
        return FeedbackClass(UNCERTAIN, None, EXCLUDED, "camera_problem_unusable")
    if kind == POLICY_ERROR:
        return FeedbackClass(POLICY_ERROR, None, EXCLUDED,
                             f"policy_error:{dismiss_reason}")
    if kind == NEGATIVE:
        if detection_type in PERSON_FAMILY_TYPES:
            return FeedbackClass(NEGATIVE, "person", PENDING, "no_person_in_frame")
        cls = OBJECT_CLASS_FOR_TYPE.get(detection_type)
        if cls is None:
            return FeedbackClass(UNCERTAIN, None, PENDING, "negative_without_class")
        return FeedbackClass(NEGATIVE, cls, PENDING, "dismissed_by_operator")
    # Dismissed with no usable reason: a reviewer decides what it is.
    target = ("person" if detection_type in PERSON_FAMILY_TYPES
              else OBJECT_CLASS_FOR_TYPE.get(detection_type))
    return FeedbackClass(UNCERTAIN, target, PENDING, "dismissed_without_reason")


class ReviewError(ValueError):
    pass


def review(db: Session, image: TrainingImage, decision: str, *,
           reviewer_id: int, kind: str | None = None,
           target_class: str | None = None) -> TrainingImage:
    """Apply a reviewer's decision. Only approve can make an image trainable.

    Approval requires a trainable kind (positive/negative) and a target
    class; a positive needs at least one box. Caller commits.
    """
    if decision not in ("approve", "reject", "exclude"):
        raise ReviewError(f"unknown decision {decision!r}")
    if kind is not None and kind not in EXAMPLE_KINDS:
        raise ReviewError(f"unknown kind {kind!r}")
    if kind is not None:
        image.example_kind = kind
    if target_class is not None:
        image.target_class = target_class or None
    extra = dict(image.source_extra or {})
    if (bool(getattr(settings, "training_review_require_different_user", False))
            and extra.get("verdict_user_id") is not None
            and int(extra["verdict_user_id"]) == int(reviewer_id)):
        raise ReviewError("a different person must review this evidence")

    if decision == "approve":
        if image.example_kind not in (POSITIVE, NEGATIVE):
            raise ReviewError("only positive or negative examples can be approved")
        if not image.target_class:
            raise ReviewError("approved evidence needs a target class")
        boxes = (db.query(func.count(Annotation.id))
                   .filter(Annotation.image_id == image.id).scalar() or 0)
        if image.example_kind == POSITIVE and boxes == 0:
            raise ReviewError("a positive example needs at least one box")
        if image.example_kind == NEGATIVE and boxes:
            raise ReviewError("a negative example must not carry boxes")
        image.review_state = APPROVED
        image.eligible_for_training = True
    else:
        image.review_state = REJECTED if decision == "reject" else EXCLUDED
        image.eligible_for_training = False
    extra.update({"reviewed_by": reviewer_id, "review_decision": decision,
                  "reviewed_at": datetime.now(timezone.utc).isoformat()})
    image.source_extra = extra
    db.flush()
    return image


def readiness(db: Session, target_class: str) -> dict:
    """Can a training job for ``target_class`` start? Counts approved only."""
    rows = dict(
        db.query(TrainingImage.example_kind, func.count(TrainingImage.id))
          .filter(TrainingImage.target_class == target_class,
                  TrainingImage.review_state == APPROVED,
                  TrainingImage.eligible_for_training.is_(True))
          .group_by(TrainingImage.example_kind).all())
    pending = (db.query(func.count(TrainingImage.id))
                 .filter(TrainingImage.target_class == target_class,
                         TrainingImage.review_state.in_(PENDING_STATES))
                 .scalar() or 0)
    pos, neg = int(rows.get(POSITIVE, 0)), int(rows.get(NEGATIVE, 0))
    min_pos = int(getattr(settings, "training_min_approved_positives", 50))
    min_neg = int(getattr(settings, "training_min_approved_negatives", 25))
    max_ratio = float(getattr(settings, "training_max_class_imbalance", 4.0))
    reasons = []
    if pos < min_pos:
        reasons.append(f"{pos} approved positives, need {min_pos}")
    if neg < min_neg:
        reasons.append(f"{neg} approved negatives, need {min_neg}")
    if pos and neg and max(pos, neg) / min(pos, neg) > max_ratio:
        reasons.append(f"positives:negatives {pos}:{neg} exceeds {max_ratio:g}:1")
    return {"target_class": target_class, "ready": not reasons,
            "approved_positives": pos, "approved_negatives": neg,
            "pending_review": int(pending), "reasons": reasons,
            "requirements": {"min_positives": min_pos, "min_negatives": min_neg,
                             "max_imbalance": max_ratio}}


def readiness_for_detection_type(db: Session, detection_type: str) -> dict:
    """Readiness for the class a detection type would train, if any."""
    cls = OBJECT_CLASS_FOR_TYPE.get(detection_type)
    if cls is None:
        return {"target_class": None, "ready": False,
                "reasons": [f"{detection_type} is a behaviour rule, not an "
                            "object class; it is not trained from feedback"],
                "approved_positives": 0, "approved_negatives": 0,
                "pending_review": 0}
    return readiness(db, cls)


def dataset_readiness(db: Session, dataset_ids: list[int]) -> dict:
    """Balanced-sample gate for manually started jobs on curated datasets.

    Positive = approved image with at least one box; negative = approved
    image with none.
    """
    ids = [int(i) for i in dataset_ids if i is not None]
    approved = (db.query(TrainingImage.id)
                  .filter(TrainingImage.dataset_id.in_(ids),
                          TrainingImage.review_state == APPROVED,
                          TrainingImage.eligible_for_training.is_(True)))
    total = approved.count()
    boxed = (approved.join(Annotation, Annotation.image_id == TrainingImage.id)
                     .distinct().count())
    pos, neg = boxed, total - boxed
    min_pos = int(getattr(settings, "training_min_approved_positives", 50))
    min_neg = int(getattr(settings, "training_min_approved_negatives", 25))
    max_ratio = float(getattr(settings, "training_max_class_imbalance", 4.0))
    reasons = []
    if pos < min_pos:
        reasons.append(f"{pos} approved positives, need {min_pos}")
    if neg < min_neg:
        reasons.append(f"{neg} approved negatives, need {min_neg}")
    if pos and neg and max(pos, neg) / min(pos, neg) > max_ratio:
        reasons.append(f"positives:negatives {pos}:{neg} exceeds {max_ratio:g}:1")
    return {"ready": not reasons, "approved_positives": pos,
            "approved_negatives": neg, "reasons": reasons}
