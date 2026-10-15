"""Background jobs that grow and expire the model-evaluation test set.

All gated by settings.eval_capture_raw_frames (they store images of
people). Run on the `alerts` worker, which mounts the thumbnails,
recordings and eval volumes. See app/eval/collect.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(settings.eval_capture_raw_frames)


@celery_app.task(name="eval.collect_reviewed", ignore_result=True)
def collect_reviewed() -> dict | None:
    if not _enabled():
        return None
    from app.database import SessionLocal
    from app.eval.collect import collect_reviewed_alerts
    with SessionLocal() as db:
        stats = collect_reviewed_alerts(
            db, root=Path(settings.eval_dir),
            target_per_category=settings.eval_target_per_type,
            clip_target=settings.eval_clip_target)
    if stats.get("added") or stats.get("clips_added"):
        log.info("eval: test set grew %s", stats)
    return stats


@celery_app.task(name="eval.sample_people", ignore_result=True)
def sample_people() -> int:
    if not _enabled():
        return 0
    import redis

    from app.database import SessionLocal
    from app.eval.collect import sample_people_frames
    from app.stream.frame_buffer import FrameBuffer
    client = redis.from_url(settings.redis_url, decode_responses=True)
    with SessionLocal() as db:
        added = sample_people_frames(db, client, FrameBuffer(), root=Path(settings.eval_dir),
                                     target=settings.eval_people_target)
    if added:
        log.info("eval: saved %d busy-hour frame(s) for people labelling", added)
    return added


@celery_app.task(name="eval.prune", ignore_result=True)
def prune() -> int:
    """Retention runs even when collection is switched off, so a test
    set never outlives settings.eval_retention_days."""
    from app.database import SessionLocal
    from app.eval.collect import prune_samples
    with SessionLocal() as db:
        removed = prune_samples(db, retention_days=settings.eval_retention_days)
    if removed:
        log.info("eval: deleted %d test-set sample(s) past retention", removed)
    return removed
