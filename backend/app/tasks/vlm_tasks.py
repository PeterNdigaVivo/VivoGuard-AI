"""VLM scene-analysis Celery task (Sprint 2.1).

Runs on the `alerts` queue, enqueued via .delay() at alert-creation
time so the cloud VLM call NEVER blocks the inference hot path. Loads
the alert's snapshot, calls the VLM, writes the description into the
DetectionEvent.extra["vlm_scene"] JSON, and bumps the store cache so
the alert card picks it up on its next poll. Best-effort throughout.
"""
from __future__ import annotations
import logging

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="vlm.analyse_alert_scene", ignore_result=True)
def analyse_alert_scene(alert_id: int) -> None:
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Alert, DetectionEvent

    if not bool(getattr(settings, "vlm_enabled", False)):
        return
    vlm_types = set(getattr(settings, "vlm_alert_types", []) or [])

    with SessionLocal() as db:
        a = db.get(Alert, alert_id)
        if a is None:
            return
        ev = db.get(DetectionEvent, a.event_id) if a.event_id else None
        if ev is None:
            return
        if ev.detection_type not in vlm_types:
            return
        if not ev.thumbnail_path:
            return
        # Skip if already analysed (idempotent — a duplicate enqueue
        # or retry must not double-call the paid API).
        if (ev.extra or {}).get("vlm_scene"):
            return

        # Build a small context dict for the prompt (duration for the
        # checkout case, store-scoped fields when handy).
        context = {}
        extra = ev.extra or {}
        if extra.get("dwell_minutes") is not None:
            mins = extra.get("dwell_minutes")
            context["duration"] = f"{mins} minute{'s' if mins != 1 else ''}"
        elif extra.get("dwell_seconds") is not None:
            context["duration"] = f"{int(extra['dwell_seconds'])} seconds"

        try:
            from app.ai.vlm_analyst import VLMAnalyst
            scene = VLMAnalyst().analyse(
                ev.thumbnail_path, ev.detection_type, context=context)
        except Exception as e:
            log.warning("vlm task: analyse raised alert=%s: %s", alert_id, e)
            scene = None

        if not scene:
            return

        # Persist into extra. SQLAlchemy JSON columns need a NEW dict
        # for the change to be detected (in-place mutation isn't
        # tracked) — rebuild it.
        ev.extra = {**(ev.extra or {}), "vlm_scene": scene}
        try:
            db.commit()
        except Exception:
            db.rollback()
            return

        # Refresh the dashboard cache so the alert card shows the
        # scene on its next poll (existing pattern — no new mechanism).
        store_id = None
        try:
            from app.models import Camera
            cam = db.get(Camera, ev.camera_id) if ev.camera_id else None
            store_id = cam.store_id if cam else None
        except Exception:
            store_id = None
        if store_id is not None:
            try:
                from app.utils.cache import bump_store_version
                bump_store_version(store_id)
            except Exception:
                pass
        log.info("vlm: scene written alert=%s type=%s chars=%d",
                 alert_id, ev.detection_type, len(scene))
