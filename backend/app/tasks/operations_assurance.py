"""Scheduled deterministic assurance agents; all outputs require human review."""
import logging
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import AssuranceCase, GovernanceAuditLog, OperationalEvent, RiskReview
from app.operations.assurance import (
    assess_coverage, correlate_event, create_alert_quality_cases,
    create_lone_worker_cases,
)
from app.tasks.celery_app import celery_app
from app.config import settings

log = logging.getLogger(__name__)


def _report(name: str, started: float, findings: dict, *, status: str = "ok", gaps=None):
    from app.tasks.agents import _heartbeat, _redis, _write_report
    _heartbeat(_redis(), name)
    _write_report(name, status, findings, gaps=gaps,
                  duration_ms=int((time.time() - started) * 1000))


@celery_app.task(name="operations.coverage_assurance", ignore_result=True)
def coverage_assurance():
    started = time.time()
    with SessionLocal() as db:
        results = assess_coverage(db, persist=True)
        db.commit()
        log.info("coverage assurance: %s requirements assessed", len(results))
    failures = [result for result in results if result["status"] != "pass"]
    _report("coverage_assurance", started, {"requirements": len(results),
            "failures": len(failures)}, status="warning" if failures else "ok",
            gaps=failures or None)


@celery_app.task(name="operations.alert_quality", ignore_result=True)
def alert_quality():
    started = time.time()
    with SessionLocal() as db:
        count = create_alert_quality_cases(db)
        db.commit()
        log.info("alert quality: %s exceptions", count)
    _report("alert_quality", started, {"exceptions": count},
            status="warning" if count else "ok",
            gaps=[{"alert_quality_cases": count}] if count else None)


@celery_app.task(name="operations.lone_worker", ignore_result=True)
def lone_worker():
    started = time.time()
    with SessionLocal() as db:
        count = create_lone_worker_cases(db)
        db.commit()
        log.info("lone worker: %s review cases", count)
    _report("lone_worker", started, {"review_cases": count},
            status="warning" if count else "ok",
            gaps=[{"review_cases": count}] if count else None)


@celery_app.task(name="operations.event_fusion", ignore_result=True)
def event_fusion():
    """Recover imported events that did not complete synchronous correlation."""
    started = time.time()
    with SessionLocal() as db:
        events = (db.query(OperationalEvent)
                  .outerjoin(RiskReview, RiskReview.operational_event_id == OperationalEvent.id)
                  .filter(RiskReview.id.is_(None))
                  .order_by(OperationalEvent.occurred_at).limit(500).all())
        for event in events:
            correlate_event(db, event)
        db.commit()
        log.info("event fusion: %s pending events correlated", len(events))
    _report("event_fusion", started, {"events_correlated": len(events)})


@celery_app.task(name="operations.extract_recall_sample", ignore_result=True)
def extract_recall_sample(case_id: int):
    """Extract one bounded blind-review clip from a retained recording."""
    from app.models import RecordingClip
    with SessionLocal() as db:
        case = db.get(AssuranceCase, case_id)
        if not case or case.case_type != "recall_sample":
            return
        evidence = dict(case.evidence or {})
        clip = db.get(RecordingClip, evidence.get("recording_clip_id"))
        recordings_root = Path(settings.recordings_dir).resolve()
        source = Path(clip.file_path).resolve() if clip and clip.file_path else None
        out_root = (recordings_root / "recall_samples").resolve()
        out = (out_root / f"{case.id}.mp4").resolve()
        if (source is None or not source.is_file()
                or recordings_root not in source.parents
                or out.parent != out_root):
            evidence["extraction_status"] = "source_unavailable"
            case.evidence = evidence
            case.status = "evidence_unavailable"
            db.commit()
            return
        out_root.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(".tmp.mp4")
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-ss", str(int(evidence["offset_seconds"])), "-i", str(source),
            "-t", str(int(evidence["duration_seconds"])),
            "-c:v", "libx264", "-preset", "veryfast", "-an",
            "-movflags", "+faststart", str(temp),
        ]
        try:
            completed = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=180, check=False,
            )
            if completed.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
                raise RuntimeError(f"ffmpeg exited {completed.returncode}")
            os.replace(temp, out)
            evidence["extraction_status"] = "ready"
            evidence["clip_bytes"] = out.stat().st_size
            case.evidence = evidence
            case.status = "pending_primary_review"
            db.add(GovernanceAuditLog(
                action="recall_sample.extracted", entity_type="assurance_case",
                entity_id=str(case.id), details={"clip_bytes": out.stat().st_size},
            ))
        except Exception as exc:
            temp.unlink(missing_ok=True)
            evidence["extraction_status"] = "failed"
            evidence["extraction_error"] = str(exc)[:128]
            case.evidence = evidence
            case.status = "evidence_unavailable"
            log.warning("recall sample extraction failed case=%s: %s", case.id, exc)
        db.commit()


@celery_app.task(name="operations.retention", ignore_result=True)
def retention():
    """Delete only closed/reviewed records after documented retention windows."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        resolved_cases = (db.query(AssuranceCase)
                          .filter(AssuranceCase.resolved_at < now - timedelta(days=365))
                          .delete(synchronize_session=False))
        reviewed_events = (db.query(OperationalEvent)
                           .filter(OperationalEvent.received_at < now - timedelta(days=365),
                                   OperationalEvent.id.in_(db.query(RiskReview.operational_event_id)
                                                          .filter(RiskReview.reviewed_at.is_not(None))))
                           .delete(synchronize_session=False))
        audit_rows = (db.query(GovernanceAuditLog)
                      .filter(GovernanceAuditLog.created_at < now - timedelta(days=730))
                      .delete(synchronize_session=False))
        db.commit()
        log.info("operations retention: cases=%s events=%s audit=%s",
                 resolved_cases, reviewed_events, audit_rows)
    # Validation clips contain ordinary CCTV footage and have a much shorter
    # purpose-limited retention than their audit records.
    clip_root = Path(settings.recordings_dir) / "recall_samples"
    cutoff = now - timedelta(days=7)
    with SessionLocal() as db:
        expired = (db.query(AssuranceCase.id)
                   .filter(AssuranceCase.case_type == "recall_sample",
                           AssuranceCase.reviewed_at < cutoff).all())
    for (case_id,) in expired:
        try:
            (clip_root / f"{case_id}.mp4").unlink(missing_ok=True)
        except OSError:
            log.warning("failed to prune recall sample clip case=%s", case_id)
