"""Staff vs customer classifier — Layer 1 of the May-2026 visitor
intelligence redesign.

Premise: real Vivo store staff spend hours behind the counter; a
customer dips into the counter zone for the time it takes to pay.
So any track that has accumulated >10 minutes of dwell inside a
counter zone today is *probably* a staff member. Persisting that
flag in `staff_tracks` lets the analytics endpoints filter staff
out of:

  • unique-visitor counts
  • dwell-time averages
  • customer-journey lists
  • visit-quality scoring

Data source: app.models.CustomerJourney. The customer-journey
detector already writes `zone_sequence_json` per track, with each
entry shaped as
    {"zone_id": int, "zone_name": str,
     "entered_at": "<iso>", "exited_at": "<iso>" | null}.
We sum the (exited_at − entered_at) seconds for every entry whose
zone's `detection_types_json` contains "counter", and threshold
at 600 seconds.

Run cadence: every 10 minutes via Celery beat. Inside one tick we
process today's data for every active store and upsert one row per
distinct track_signature. Cheap — <1s per store at fleet scale.

FUTURE UPGRADE PATH
  • Uniform-detection model: once a YOLO model with classes
    `uniform_ok` / `uniform_violation` is trained, this task can
    also positively flag staff at detection-time. Accuracy jumps
    from ~85% (spatial heuristic) to ~97%.
  • Face recognition: enrolled staff faces give us a deterministic
    classifier with near-100% precision. Same row schema in
    staff_tracks; just a stronger evidence source.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


# Threshold: track spent >COUNTER_DWELL_THRESHOLD seconds inside a
# counter zone today → flagged as staff. 10 minutes per the spec.
COUNTER_DWELL_THRESHOLD = 600


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Python's fromisoformat handles trailing Z in 3.11+; strip
        # for safety on older builds.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


@celery_app.task(name="staff_classifier.classify_today", ignore_result=True)
def classify_today() -> None:
    """Walk today's customer_journeys for every active store, sum
    counter-zone seconds per track, upsert staff_tracks rows."""
    from app.database import SessionLocal
    from app.models import Camera, CustomerJourney, StaffTrack, Store, Zone

    today = datetime.now(timezone.utc).date()
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.is_active == True).all()  # noqa: E712

        # Build a set of "counter zone IDs" up front. A zone counts as
        # a counter zone iff its detection_types_json contains "counter".
        all_zones = db.query(Zone).all()
        counter_zone_ids: set[int] = {
            z.id for z in all_zones
            if "counter" in (z.detection_types_json or [])
        }
        if not counter_zone_ids:
            log.info("staff_classifier: no counter zones configured fleet-wide — skipping")
            return

        total_upserts = 0
        for store in stores:
            cam_ids = [c.id for c in db.query(Camera).filter(Camera.store_id == store.id).all()]
            if not cam_ids:
                continue

            journeys = (
                db.query(CustomerJourney)
                  .filter(CustomerJourney.store_id == store.id,
                          CustomerJourney.day == today)
                  .all()
            )

            # Aggregate counter-zone seconds + first/last seen per
            # track_signature. Multiple journey rows can exist per
            # track if the detector flushes mid-day; we union them.
            agg: dict[str, dict] = {}
            for j in journeys:
                sig = j.track_signature
                if not sig:
                    continue
                entry = agg.setdefault(sig, {
                    "counter_seconds": 0,
                    "first_seen": j.started_at,
                    "last_seen":  j.ended_at or j.started_at,
                })
                if j.started_at and entry["first_seen"] and j.started_at < entry["first_seen"]:
                    entry["first_seen"] = j.started_at
                last_candidate = j.ended_at or j.started_at
                if last_candidate and entry["last_seen"] and last_candidate > entry["last_seen"]:
                    entry["last_seen"] = last_candidate

                for step in (j.zone_sequence_json or []):
                    if not isinstance(step, dict):
                        continue
                    zid = step.get("zone_id")
                    if zid not in counter_zone_ids:
                        continue
                    enter = _parse_iso(step.get("entered_at"))
                    exit_ = _parse_iso(step.get("exited_at"))
                    if not enter or not exit_:
                        continue
                    dur = int((exit_ - enter).total_seconds())
                    if dur > 0:
                        entry["counter_seconds"] += dur

            # Upsert one row per track. Postgres ON CONFLICT lets us
            # bump counter_seconds + classification without a SELECT
            # roundtrip per row.
            for sig, e in agg.items():
                classified = "staff" if e["counter_seconds"] >= COUNTER_DWELL_THRESHOLD else "customer"
                stmt = (pg_insert(StaffTrack.__table__)
                        .values(store_id=store.id, day=today, track_signature=sig,
                                first_seen=e["first_seen"], last_seen=e["last_seen"],
                                counter_seconds=e["counter_seconds"],
                                classified_as=classified)
                        .on_conflict_do_update(
                            constraint="uq_staff_tracks_store_day_sig",
                            set_={
                                "counter_seconds": e["counter_seconds"],
                                "last_seen":       e["last_seen"],
                                "classified_as":   classified,
                            },
                        ))
                db.execute(stmt)
                total_upserts += 1
        db.commit()
        log.info("staff_classifier: %d upserts across %d stores for %s",
                 total_upserts, len(stores), today)
