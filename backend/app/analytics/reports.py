"""Reporting helpers: campaign lift + multi-store comparison + PDF/CSV.

PDFs use reportlab (added in requirements.api.txt). It has no system
deps so it works inside the slim API image without bloating it (~6 MB).
"""
from __future__ import annotations
import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Alert, Camera, Campaign, DetectionEvent, MetricSnapshot, Store, VisitorTrack,
)


# ---------------------------------------------------------------------------
# Campaign lift
# ---------------------------------------------------------------------------

def campaign_lift(db: Session, campaign_id: int, *,
                  metric_type: str = "passersby") -> dict:
    """Compare the mean value of `metric_type` in three windows:
        before  — N days immediately before the campaign starts
        during  — the campaign window itself
        after   — N days immediately after
    where N = the campaign's own length. Lift is reported as
    `(during − before) / before * 100`.
    """
    c = db.get(Campaign, campaign_id)
    if not c:
        raise ValueError("campaign not found")

    span_days = max(1, (c.end_date - c.start_date).days)
    pre_start  = datetime.combine(c.start_date - timedelta(days=span_days), datetime.min.time(), tzinfo=timezone.utc)
    pre_end    = datetime.combine(c.start_date, datetime.min.time(), tzinfo=timezone.utc)
    dur_start  = pre_end
    dur_end    = datetime.combine(c.end_date,   datetime.max.time(), tzinfo=timezone.utc)
    post_start = dur_end
    post_end   = post_start + timedelta(days=span_days)

    def _avg(t0: datetime, t1: datetime) -> float | None:
        q = (db.query(func.avg(MetricSnapshot.value))
               .filter(MetricSnapshot.metric_type == metric_type,
                       MetricSnapshot.period_start >= t0,
                       MetricSnapshot.period_start <  t1))
        if c.store_id is not None:
            q = q.filter(MetricSnapshot.store_id == c.store_id)
        v = q.scalar()
        return float(v) if v is not None else None

    before = _avg(pre_start, pre_end)
    during = _avg(dur_start, dur_end)
    after  = _avg(post_start, post_end)

    def _lift(a: float | None, b: float | None) -> float | None:
        if a is None or b in (None, 0):
            return None
        return (a - b) / b * 100.0

    return {
        "campaign_id": c.id, "name": c.name, "store_id": c.store_id,
        "metric_type": metric_type, "span_days": span_days,
        "before": before, "during": during, "after": after,
        "lift_during_vs_before_pct": _lift(during, before),
        "lift_after_vs_before_pct":  _lift(after, before),
    }


# ---------------------------------------------------------------------------
# Per-store rollup (shared by CSV + PDF)
# ---------------------------------------------------------------------------

def store_rollup(db: Session, store_id: int, *,
                 since: datetime, until: datetime) -> dict:
    store = db.get(Store, store_id)
    if not store:
        raise ValueError("store not found")

    def _avg(metric: str) -> float | None:
        v = (db.query(func.avg(MetricSnapshot.value))
               .filter(MetricSnapshot.store_id == store_id,
                       MetricSnapshot.metric_type == metric,
                       MetricSnapshot.period_start >= since,
                       MetricSnapshot.period_start <= until)
               .scalar())
        return float(v) if v is not None else None

    unique_visitors = (db.query(func.count(VisitorTrack.id))
                         .filter(VisitorTrack.store_id == store_id,
                                 VisitorTrack.first_seen >= since,
                                 VisitorTrack.first_seen <= until)
                         .scalar() or 0)

    alerts_by_type = dict(
        db.query(DetectionEvent.detection_type, func.count(Alert.id))
          .join(Alert, Alert.event_id == DetectionEvent.id)
          .join(Camera, Camera.id == DetectionEvent.camera_id)
          .filter(Camera.store_id == store_id,
                  Alert.created_at >= since,
                  Alert.created_at <= until)
          .group_by(DetectionEvent.detection_type)
          .all()
    )

    return {
        "store_id": store.id, "store_name": store.name, "country": store.country,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "kpis": {
            "occupancy_avg":         _avg("occupancy"),
            "queue_length_avg":      _avg("queue_length"),
            "queue_wait_avg_sec":    _avg("queue_wait_seconds"),
            "staff_present_pct_avg": _avg("staff_present_pct"),
            "unique_visitors":       int(unique_visitors),
            "passersby_avg":         _avg("passersby"),
            "stop_rate_avg":         _avg("stop_rate"),
            "dwell_seconds_avg":     _avg("dwell_seconds"),
            "uniform_compliance_pct_avg": _avg("uniform_compliance_pct"),
        },
        "alerts_by_type": alerts_by_type,
    }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def stores_csv(rollups: Iterable[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "store_id", "store_name", "country",
        "occupancy_avg", "queue_length_avg", "queue_wait_avg_sec",
        "staff_present_pct_avg", "unique_visitors",
        "passersby_avg", "stop_rate_avg", "dwell_seconds_avg",
        "uniform_compliance_pct_avg",
        "alerts_total",
    ])
    for r in rollups:
        k = r["kpis"]
        w.writerow([
            r["store_id"], r["store_name"], r["country"],
            k["occupancy_avg"], k["queue_length_avg"], k["queue_wait_avg_sec"],
            k["staff_present_pct_avg"], k["unique_visitors"],
            k["passersby_avg"], k["stop_rate_avg"], k["dwell_seconds_avg"],
            k["uniform_compliance_pct_avg"],
            sum(r["alerts_by_type"].values()),
        ])
    return buf.getvalue().encode()


# ---------------------------------------------------------------------------
# PDF — reportlab
# ---------------------------------------------------------------------------

def stores_pdf(title: str, rollups: list[dict]) -> bytes:
    """Render a one-page-per-store + chain summary PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm)
    styles = getSampleStyleSheet()
    story: list = []

    story.append(Paragraph(title, styles["Title"]))
    story.append(Paragraph(
        f"Generated {datetime.utcnow().isoformat(timespec='seconds')}Z",
        styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    # Chain summary table.
    header = ["Store", "Country", "Occupancy", "Queue", "Staff %",
              "Unique visitors", "Stop rate %", "Alerts"]
    rows: list[list] = [header]
    for r in rollups:
        k = r["kpis"]
        rows.append([
            r["store_name"], r["country"],
            _fmt(k["occupancy_avg"], 0),
            _fmt(k["queue_length_avg"], 1),
            _fmt((k["staff_present_pct_avg"] or 0) * 100, 0, "%"),
            str(k["unique_visitors"]),
            _fmt((k["stop_rate_avg"] or 0) * 100, 0, "%"),
            str(sum(r["alerts_by_type"].values())),
        ])
    t = Table(rows, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5e9")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
    ]))
    story.append(t)

    # Per-store page.
    for r in rollups:
        story.append(PageBreak())
        story.append(Paragraph(f"{r['store_name']} — {r['country']}", styles["Heading2"]))
        story.append(Paragraph(
            f"Window: {r['window']['since']} → {r['window']['until']}",
            styles["Italic"]))
        story.append(Spacer(1, 4 * mm))
        kpi_rows = [["Metric", "Value"]]
        for k, v in r["kpis"].items():
            kpi_rows.append([k.replace("_", " "), _fmt(v, 2)])
        kt = Table(kpi_rows, colWidths=[70 * mm, 50 * mm])
        kt.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5e9")),
            ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
            ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ]))
        story.append(kt)
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("Alerts by type", styles["Heading3"]))
        ab_rows = [["Type", "Count"]] + [[k, str(v)] for k, v in r["alerts_by_type"].items()]
        ab = Table(ab_rows, colWidths=[70 * mm, 30 * mm])
        ab.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
        story.append(ab)

    doc.build(story)
    return buf.getvalue()


def _fmt(v, digits: int = 1, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except Exception:
        return str(v)
