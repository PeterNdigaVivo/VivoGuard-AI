"""Continuous learning — turn confirmed/dismissed alerts into training data.

Confirmed alert → add the event's frame to the *positive retraining pool*
                  with the same bbox/label.
Dismissed alert → add the event's frame to the *negative retraining pool*
                  (TrainingImage with NO Annotation rows = explicit
                  background sample in YOLO format), so the next
                  fine-tune learns "this is NOT a {detection_type}".

Both pools are stored as Datasets named:
  feedback-<detection_type>              ← positives (legacy name kept)
  feedback-negative-<detection_type>     ← hard negatives (new)

Naming convention is deliberate: the trainer joins both pools into
one dataset for fine-tuning, with negatives kept at a max of
3 × positives (the standard hard-negative ratio that avoids
swamping the gradient with background examples).
"""
from __future__ import annotations
import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    Alert, Annotation, Dataset, DetectionEvent, TrainingImage,
)

log = logging.getLogger(__name__)


def _persist_feedback_image(dataset_id: int, alert_id: int,
                            source_path: str) -> str | None:
    """Copy alert evidence into the durable dataset volume.

    Alert thumbnails are retention-managed and may be deleted before a
    training job runs.  TrainingImage must therefore reference its own copy,
    not the transient alert snapshot.
    """
    from app.training.dataset import dataset_root

    source = Path(source_path)
    if not source.is_file():
        return None
    suffix = source.suffix.lower() or ".jpg"
    target = dataset_root(dataset_id) / "images" / f"alert_{alert_id}{suffix}"
    try:
        if not target.exists():
            shutil.copy2(source, target)
    except OSError as exc:
        log.warning("feedback: could not preserve alert %s image %s: %s",
                    alert_id, source, exc)
        return None
    return str(target)


def _training_provenance(verdict: str, detection_type: str = "",
                         dismiss_reason: str | None = None) -> dict:
    """Provenance state for operator feedback.

    Policy switch (Aug 2026): the codex data-integrity work quarantined
    ALL single-reviewer feedback (eligible_for_training=False,
    review_state=pending) until two independent reviewers agree — but
    Vivo runs a single-operator review workflow, so that gate starved
    training to zero (jobs 807/808/809; pseudo-labeler labelled=0).
    settings.training_require_dual_review (default False) now decides:
      False → clicks are trainable immediately (pre-codex behaviour);
      True  → the codex quarantine applies unchanged.
    source_kind always records the truth either way.

    Superseded (Nov 2026): a click is evidence, not a label. Every verdict
    now enters the review queue (pending_review, or excluded when it can
    never be trainable: behaviour rules, policy errors, camera problems)
    and only a reviewer's approval makes it trainable. See
    app.services.training_evidence.
    """
    from app.services.training_evidence import classify_feedback
    kind = ("operator_dismissed" if verdict == "false"
            else "operator_confirmed")
    fc = classify_feedback(verdict, detection_type, dismiss_reason)
    return {"source_kind": kind,
            "eligible_for_training": False,
            "review_state": fc.review_state,
            "example_kind": fc.kind,
            "target_class": fc.target_class}


def _build_source_extra(db: Session, ev: DetectionEvent,
                         alert: Alert, verdict: str) -> dict:
    """Snapshot every piece of evidence the spec asks us to persist
    next to the feedback frame: alert id + verdict, detection type,
    store + camera + zone names, timestamp, confidence, bbox, plus
    whatever sub-payload the detector dumped into DetectionEvent.extra
    (person_count, tracking_ids, …)."""
    from app.models import Camera, Zone, Store
    cam = db.get(Camera, ev.camera_id) if ev.camera_id else None
    zone = db.get(Zone, ev.zone_id) if ev.zone_id else None
    store_id = getattr(cam, "store_id", None) if cam else None
    store_name = None
    if store_id:
        s = db.get(Store, store_id)
        store_name = s.name if s else None
    ts = ev.timestamp
    return {
        "alert_id":         alert.id,
        "event_id":         ev.id,
        "verdict":          verdict,        # "correct" | "false"
        "detection_type":   ev.detection_type,
        "camera_id":        ev.camera_id,
        "camera_name":      cam.name if cam else None,
        "store_id":         store_id,
        "store_name":       store_name,
        "zone_id":          ev.zone_id,
        "zone_name":        zone.name if zone else None,
        "confidence":       ev.confidence,
        "bbox_norm":        ev.bbox_json,
        "timestamp_iso":    ts.isoformat() if ts else None,
        "model_id":         ev.model_id,
        # What the operator TYPED, carried next to what they clicked.
        # A "false" verdict on its own says the frame was wrong; the
        # note is the only place the reason survives ("reflection in
        # the glass", "that's the cleaner"), and it is what a curator
        # or a VLM prompt revision can actually act on. Travels with
        # the sample so neither has to re-join back to the alert.
        "operator_note":    (alert.notes or "").strip() or None,
        # DetectionEvent.extra already carries detector-specific
        # context (person_count, tracking_ids, queue_length, …) —
        # carry it through unchanged so curators can dedup/filter
        # without re-joining back to the event row.
        "detector_extra":   ev.extra or {},
    }


# Verdicts on these detection types never become training data.
#
# They are judgements about a whole scene or a system state, not an
# object with a bounding box. `scene_review` and `phone_usage` are
# written by a VLM reading a frame; when an operator confirms one, they
# are telling us the PROMPT was right, not that a detector should learn
# to find "scene_review" in an image. Left unguarded, the loop created
# feedback-scene_review and feedback-phone_usage datasets within hours
# of each detector going live and queued YOLO fine-tunes against a class
# that has no visual form.
#
# The others are counters, heartbeats and BI summaries whose snapshots
# carry no learnable target either. `live_activity` is deliberately NOT
# here: absorb_dismissed has special handling that redirects it to
# feedback-negative-person, which is genuinely useful.
NON_TRAINABLE_TYPES = frozenset({
    "scene_review", "phone_usage",
    "store_intelligence", "sales_floor_insight", "positive_operational",
    "system_health", "camera_offline", "shop_open_close",
    # Count- and sound-based judgements. These have no visual target a
    # detector could be trained against, and feeding them in teaches the
    # model nonsense about whole frames:
    #   audio_distress - fires on a MICROPHONE. Its bbox is [0,0,1,1]
    #     because sound has no location, so a "false" verdict would train
    #     YOLO that an entire shop floor is not-a-thing.
    #   crowd - the bbox is a synthetic +/-0.1 box around the centroid of
    #     several people, not the boundary of any object. The judgement is
    #     "how many people, for how long", which is arithmetic, not
    #     appearance.
    "audio_distress", "crowd",
})


def _abandon(alert, db: Session, reason: str) -> None:
    """Mark a verdict as handled when no training sample can ever be
    built from it.

    The operator's verdict still stands — this only closes the training
    side. Stamping is the whole point: `feedback.absorb_pending` selects
    on `feedback_used_for_training is False`, so an alert that returns
    here unstamped is re-queued every 15 minutes for as long as it sits
    inside the 7-day sweep window. Use ONLY for failures that retrying
    cannot fix; transient errors must raise so the task retries.
    """
    alert.feedback_used_for_training = True
    db.commit()
    log.warning("feedback: alert %s verdict kept, no training sample — %s",
                alert.id, reason)


def _skip_non_trainable(db: Session, alert, detection_type: str) -> bool:
    """Stamp and skip an alert whose verdict is not training data.

    Stamping matters: without it every future pass retries the same
    alert forever, because nothing else marks it as handled.
    """
    if detection_type not in NON_TRAINABLE_TYPES:
        return False
    alert.feedback_used_for_training = True
    db.commit()
    log.info("feedback: %s verdict on alert %s is not trainable — "
             "skipped (scene-level judgement, no visual target)",
             detection_type, alert.id)
    return True


def _ensure_dataset(db: Session, name: str, classes: list[str],
                    description: str) -> Dataset:
    ds = db.query(Dataset).filter(Dataset.name == name).first()
    if not ds:
        ds = Dataset(name=name, description=description, classes_json=classes)
        db.add(ds)
        db.commit()
        db.refresh(ds)
        return ds
    # Union classes — adding a new detection sub-class shouldn't shrink the
    # set of labels the dataset already knows about.
    if classes:
        cur = set(ds.classes_json or [])
        for c in classes:
            cur.add(c)
        ds.classes_json = sorted(cur)
        db.commit()
    return ds


# Kept for backwards-compat imports; the aggressive schedule (Aug 2026)
# applies the immediate check to EVERY detection type, so this set no
# longer gates anything.
IMMEDIATE_RETRAIN_TYPES = {
    "uniform_compliance", "shop_open_close", "checkout_dwell",
    "staff_present", "trespass", "intrusion", "person",
}


def _maybe_enqueue_training(db: Session, detection_type: str) -> None:
    """Aggressive feedback-driven scheduling (Aug 2026): every click
    counts new samples since the last COMPLETED job for this type —
    >= feedback_full_retrain_after (30)  → queue a FULL retrain
    >= feedback_finetune_after (10)      → queue a fine-tune
    Skips when a queued/running job for the type already exists, so
    clicks 11..N while a job is pending don't pile up duplicates.
    Best-effort — the feedback save matters more than the training
    trigger, so never raise."""
    try:
        from app.config import settings
        from app.training.orchestrator import (
            _last_fine_tune_completion, _new_samples_since,
            enqueue_fine_tune_if_due, enqueue_full_retrain, has_open_job,
        )
        if has_open_job(db, detection_type):
            return
        fine_after = int(getattr(settings, "feedback_finetune_after", 10))
        full_after = int(getattr(settings, "feedback_full_retrain_after", 30))
        since = _last_fine_tune_completion(db, detection_type)
        pos_new, neg_new = _new_samples_since(db, detection_type, since)
        clicks = pos_new + neg_new
        if clicks >= full_after:
            enqueue_full_retrain(db, detection_type)
        elif clicks >= fine_after:
            enqueue_fine_tune_if_due(db, detection_type, min_new=fine_after)
    except Exception as e:
        log.warning("training enqueue failed: %s", e)


def _enqueue_if_trainable(db: Session, detection_type: str,
                          provenance: dict) -> None:
    """Evaluate retraining immediately for feedback allowed by policy."""
    if provenance.get("eligible_for_training"):
        _maybe_enqueue_training(db, detection_type)


def _enqueue_temporal_harvest(alert_id: int) -> None:
    """Kick the ±1s temporal-context frame extraction onto the WORKER
    (the clip lives on the shared volume and opencv only exists there).
    Best-effort — never raises."""
    try:
        from app.tasks.feedback_harvest import harvest_temporal_frames
        harvest_temporal_frames.delay(alert_id)
    except Exception as e:
        log.warning("temporal harvest enqueue failed alert=%s: %s",
                    alert_id, e)


def _enqueue_preview(image_id: int) -> None:
    """Kick the orange-box preview generation onto the WORKER (opencv lives
    there — the API process that runs the absorb_* helpers does not have it,
    which is why every preview_path was NULL). Best-effort — never raises."""
    try:
        from app.tasks.training import write_preview_for_image
        write_preview_for_image.delay(image_id)
    except Exception as e:
        log.warning("preview enqueue failed image=%s: %s", image_id, e)


def absorb_label(db: Session, alert_id: int, mode: str) -> None:
    """Turn one operator mark into the alert's single training row.

    ``mode`` is "confirm" (True), "dismiss" (False, reason from
    alert.dismiss_reason) or "exclude" (Unclear / Bad image buttons).

    One row per alert (TrainingImage.label_alert_id is unique): a re-mark
    updates this row — kind, pool, boxes and promotion decision — instead
    of adding another. Whether it trains is decided by
    app.services.label_promotion (trust/agreement gate, burst guard, audit
    sample); every decision is recorded on the row and in the governance
    audit log. Never queues a training job.
    """
    import os as _os

    from app.services import label_promotion as lp
    from app.services.training_evidence import POSITIVE, classify_feedback

    a = db.get(Alert, alert_id)
    if not a or a.feedback_used_for_training:
        return
    ev = db.get(DetectionEvent, a.event_id)
    if not ev or not ev.thumbnail_path:
        _abandon(a, db, "no snapshot was saved for the event")
        return
    cls = ev.detection_type
    if _skip_non_trainable(db, a, cls):
        return

    verdict = "correct" if mode == "confirm" else "false"
    fc = classify_feedback(verdict, cls,
                           None if mode == "confirm" else a.dismiss_reason)
    decision = lp.latest_decision(db, a.id) if mode != "exclude" else None
    user_id = decision.reviewer_id if decision is not None else a.assigned_to
    from app.models import User
    user = db.get(User, user_id) if user_id else None
    marked_at = lp.marked_at_for(decision)

    # live_activity snapshots have boxes burned in; use the raw frame.
    src = ev.thumbnail_path
    if cls == "live_activity":
        raw = (ev.extra or {}).get("raw_snapshot_path")
        if not raw or not _os.path.exists(raw):
            _abandon(a, db, "live_activity has no raw snapshot")
            return
        src = raw

    positive = fc.kind == POSITIVE and fc.target_class is not None
    pool = (f"feedback-{fc.target_class}" if positive
            else f"feedback-negative-{fc.target_class or cls}")
    ds = _ensure_dataset(
        db, pool, [fc.target_class] if positive else [],
        description=("auto: operator-confirmed labels (positive pool)" if positive
                     else "auto: operator labels without boxes (negative pool)"))

    bbox = list(ev.bbox_json or [])
    if positive and len(bbox) != 4:
        _abandon(a, db, f"event bbox is unusable ({ev.bbox_json!r})")
        return

    img = (db.query(TrainingImage)
             .filter(TrainingImage.label_alert_id == a.id).one_or_none())
    created = img is None
    if created:
        file_path = _persist_feedback_image(ds.id, a.id, src)
        if not file_path:
            _abandon(a, db, "the snapshot file is no longer on disk")
            return
        img = TrainingImage(dataset_id=ds.id, camera_id=ev.camera_id,
                            file_path=file_path, labeled=True,
                            source_alert_id=a.id, label_alert_id=a.id)
        db.add(img)
    else:
        img.dataset_id = ds.id       # a re-mark can move it between pools
    src_extra = _build_source_extra(db, ev, a, "correct" if mode == "confirm" else "false")
    src_extra.update({"source_thumbnail_path": src, "verdict_user_id": user_id,
                      "dismiss_reason": a.dismiss_reason if mode != "confirm" else None,
                      "mark": mode})
    img.source_extra = src_extra
    img.source_kind = "operator_confirmed" if mode == "confirm" else "operator_dismissed"
    img.example_kind = fc.kind
    img.target_class = fc.target_class
    db.flush()

    db.query(Annotation).filter(Annotation.image_id == img.id).delete()
    if positive:
        x1, y1, x2, y2 = bbox
        db.add(Annotation(image_id=img.id, class_label=fc.target_class,
                          bbox_json=[(x1 + x2) / 2, (y1 + y2) / 2,
                                     max(0.0, x2 - x1), max(0.0, y2 - y1)],
                          verified=True))

    result = lp.decide(
        db, fc, user=user,
        verdict=(decision.verdict if decision is not None else None),
        marked_at=marked_at, ai_verdict=a.ai_verdict,
        ai_confidence=a.ai_confidence, detector_confidence=ev.confidence)
    lp.apply(img, result, user_id=user_id, marked_at=marked_at,
             extra={"mark": mode, "detection_type": cls})
    db.flush()
    if result.path == "held_burst":
        held = lp.hold_earlier_burst(db, user_id=user_id, kind=fc.kind,
                                     at=marked_at, exclude_id=img.id)
        img.gate_json = {**(img.gate_json or {}), "earlier_marks_held": held}
    lp.audit(db, img, user=user, alert_id=a.id, mark=mode)
    a.feedback_used_for_training = True
    db.commit()
    if created:
        _enqueue_preview(img.id)
        if positive:
            _enqueue_temporal_harvest(alert_id)
    log.info("feedback: alert %s %s -> %s (%s %s)", alert_id, mode,
             img.promotion_path, fc.kind, fc.target_class)


def absorb_confirmed(db: Session, alert_id: int) -> None:
    """Operator marked the alert True (see absorb_label)."""
    absorb_label(db, alert_id, "confirm")


def absorb_dismissed(db: Session, alert_id: int) -> None:
    """Operator marked the alert False (see absorb_label)."""
    absorb_label(db, alert_id, "dismiss")


# Backwards-compat alias — old call sites still import `mark_dismissed`.
def mark_dismissed(db: Session, alert_id: int) -> None:
    absorb_dismissed(db, alert_id)


def pending_retraining_count(db: Session, detection_type: str) -> dict:
    """How many positives + negatives are waiting in the feedback pool
    for this detection type. Used by the metrics endpoint and the
    chain-retrain trigger to decide if there's enough new signal to
    justify a fine-tune."""
    out = {"positives": 0, "negatives": 0}
    pairs = (("positives", f"feedback-{detection_type}"),
             ("negatives", f"feedback-negative-{detection_type}"))
    for kind, ds_name in pairs:
        ds = db.query(Dataset).filter(Dataset.name == ds_name).first()
        if ds is not None:
            out[kind] = (db.query(TrainingImage)
                            .filter(TrainingImage.dataset_id == ds.id).count())
    return out


def revert_verdict(db: Session, alert_id: int) -> bool:
    """Delete the TrainingImage row(s) created by absorb_confirmed /
    mark_dismissed for this alert.

    Called by the Part 5 sprint /undo endpoint when an operator
    reverses a verdict. Returns True when at least one image was
    deleted, False when the alert had no associated training images
    (the absorb call may have failed silently when the verdict was
    originally recorded — see the log.exception path in
    services/alert_feedback.record_verdict — OR the alert predates
    migration 0025 so its image has source_alert_id=NULL).

    Annotation rows hanging off each TrainingImage CASCADE on delete
    via the existing FK (models/training.py — Annotation.image_id
    ondelete=CASCADE). No manual annotation cleanup needed.

    Also clears the alert's feedback_used_for_training flag so the
    next labelling attempt (either direction) is not a no-op on the
    absorb_* side.
    """
    imgs = (db.query(TrainingImage)
              .filter(TrainingImage.source_alert_id == alert_id)
              .all())
    a = db.get(Alert, alert_id)
    if not imgs:
        # Still flip the alert flag — the operator's verdict was
        # already reversed by the sprint endpoint; we just have
        # nothing to clean up in the pool.
        if a is not None and a.feedback_used_for_training:
            a.feedback_used_for_training = False
            db.commit()
        return False

    # The label row is kept for provenance (and so a re-mark updates it)
    # but can no longer train. Other rows derived from the alert
    # (harvested context frames) are removed as before.
    for img in imgs:
        if img.label_alert_id == alert_id:
            img.eligible_for_training = False
            img.review_state = "excluded"
            img.promotion_path = "reverted"
        else:
            db.delete(img)        # Annotations CASCADE via FK
    if a is not None:
        a.feedback_used_for_training = False
    db.commit()
    log.info("feedback: reverted verdict alert=%s training_images=%d",
             alert_id, len(imgs))
    return True
