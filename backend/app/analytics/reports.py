"""Campaign lift analytics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Campaign, MetricSnapshot


def campaign_lift(
    db: Session,
    campaign_id: int,
    *,
    metric_type: str = "passersby",
) -> dict:
    """Compare mean metric values before, during, and after a campaign."""
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("campaign not found")

    span_days = max(1, (campaign.end_date - campaign.start_date).days)
    during_start = datetime.combine(
        campaign.start_date, datetime.min.time(), tzinfo=timezone.utc,
    )
    during_end = datetime.combine(
        campaign.end_date, datetime.max.time(), tzinfo=timezone.utc,
    )

    def average(start: datetime, end: datetime) -> float | None:
        query = db.query(func.avg(MetricSnapshot.value)).filter(
            MetricSnapshot.metric_type == metric_type,
            MetricSnapshot.period_start >= start,
            MetricSnapshot.period_start < end,
        )
        if campaign.store_id is not None:
            query = query.filter(MetricSnapshot.store_id == campaign.store_id)
        value = query.scalar()
        return float(value) if value is not None else None

    before = average(during_start - timedelta(days=span_days), during_start)
    during = average(during_start, during_end)
    after = average(during_end, during_end + timedelta(days=span_days))

    def percentage_change(value: float | None, baseline: float | None) -> float | None:
        if value is None or baseline in (None, 0):
            return None
        return (value - baseline) / baseline * 100.0

    return {
        "campaign_id": campaign.id,
        "name": campaign.name,
        "store_id": campaign.store_id,
        "metric_type": metric_type,
        "span_days": span_days,
        "before": before,
        "during": during,
        "after": after,
        "lift_during_vs_before_pct": percentage_change(during, before),
        "lift_after_vs_before_pct": percentage_change(after, before),
    }
