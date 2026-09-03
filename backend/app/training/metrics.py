"""Per-model drift metrics derived from operator feedback.

Aggregates Alert rows by (DetectionEvent.model_id, detection_type,
day) and writes one ModelMetric row per group. confirmed → TP,
dismissed → FP. Alerts still in 'new' / 'escalated' are excluded
from the precision math (no operator label yet) but counted in
alerts_total so the dashboard can show "x% of today's alerts have
been triaged."

Recall is not derivable from operator marks alone — there's no
record of detections the model MISSED. The column on ModelMetric
is reserved for Phase 3 (shadow deployment), where the candidate
model's hits are compared against production's.
"""
from __future__ import annotations
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Alert, DetectionEvent, ModelMetric

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Nairobi")


def _eat_day_bounds_utc(day_eat: date) -> tuple[datetime, datetime]:
    """[start, end) UTC instants for the given EAT calendar day."""
    start_eat = datetime(day_eat.year, day_eat.month, day_eat.day,
                          0, 0, 0, tzinfo=EAT)
    end_eat   = start_eat + timedelta(days=1)
    return (start_eat.astimezone(timezone.utc),
            end_eat.astimezone(timezone.utc))


def compute_metrics_for_day(db: Session, day_eat: date) -> int:
    """Recompute and upsert ModelMetric rows for every
    (model_id, detection_type) bucket that had at least one alert on
    the given EAT day. Returns the number of buckets upserted.

    Idempotent — safe to re-run after late operator marks land."""
    start_utc, end_utc = _eat_day_bounds_utc(day_eat)
    rows = (db.query(DetectionEvent.model_id,
                      DetectionEvent.detection_type,
                      Alert.status,
                      func.count(Alert.id))
              .join(Alert, Alert.event_id == DetectionEvent.id)
              .filter(Alert.created_at >= start_utc,
                      Alert.created_at <  end_utc)
              .group_by(DetectionEvent.model_id,
                         DetectionEvent.detection_type,
                         Alert.status)
              .all())

    # Re-shape into {(model_id, detection_type): {status: count}}
    buckets: dict[tuple[int | None, str], dict[str, int]] = {}
    for model_id, det_type, status, n in rows:
        if det_type is None:
            continue
        key = (model_id, det_type)
        buckets.setdefault(key, {})[status] = int(n)

    upserts = 0
    for (model_id, det_type), counts in buckets.items():
        tp     = counts.get("confirmed", 0)
        fp     = counts.get("dismissed", 0)
        total  = sum(counts.values())
        triaged = tp + fp
        precision = (tp / triaged) if triaged else None
        # This is the false share among reviewed alerts. Using all alerts as
        # the denominator made an unreviewed backlog artificially improve it.
        fp_rate   = (fp / triaged) if triaged else None

        # Upsert via lookup-then-update — simpler and DB-agnostic
        # than ON CONFLICT, and the (model_id, detection_type, day)
        # composite is small enough that the cost is fine.
        mm = (db.query(ModelMetric)
                .filter(ModelMetric.model_id == model_id,
                        ModelMetric.detection_type == det_type,
                        ModelMetric.day == day_eat)
                .one_or_none())
        if mm is None:
            mm = ModelMetric(model_id=model_id,
                              detection_type=det_type,
                              day=day_eat)
            db.add(mm)
        mm.tp_count      = tp
        mm.fp_count      = fp
        mm.alerts_total  = total
        mm.precision     = precision
        mm.fp_rate       = fp_rate
        mm.computed_at   = datetime.now(timezone.utc)
        upserts += 1
    db.commit()
    log.info("model_metrics: day=%s upserts=%d", day_eat.isoformat(), upserts)
    return upserts


def backfill_recent_days(db: Session, days: int = 7) -> int:
    """Rebuild metrics for the last `days` EAT days. Useful after a
    bulk operator triage session adds late labels to old alerts."""
    today_eat = datetime.now(timezone.utc).astimezone(EAT).date()
    total = 0
    for d in range(days):
        total += compute_metrics_for_day(db, today_eat - timedelta(days=d))
    return total
