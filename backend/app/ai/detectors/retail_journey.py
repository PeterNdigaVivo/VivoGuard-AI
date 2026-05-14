"""Customer journey mapping.

For each person track, record the ordered sequence of zones it passes
through during its lifetime on a given camera (entry → aisle1 → aisle2
→ counter → exit, etc.). On track expiry (no longer in current
detections), flush the journey to the `customer_journeys` table.

Operators don't need to enable anything per camera — this detector is
treated as always-on by the inference loader for any camera with
ai_enabled=True. It only writes journeys longer than 2 zones (anything
shorter is noise).
"""
from __future__ import annotations
import time
from datetime import date, datetime, timezone

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone


class CustomerJourneyDetector(Detector):
    detection_type = "customer_journey"
    needs_tracking = True

    def __init__(self):
        # track_id → {"zones": [{...}], "started_at": ts, "current_zone": id|None}
        self._open: dict[int, dict] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        if ctx.db is None:
            return []
        from app.models import CustomerJourney

        now = time.time()
        active_track_ids: set[int] = set()

        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            active_track_ids.add(tr.track_id)
            # Find which (if any) zone the centroid lies in.
            current_zone = None
            for z in ctx.zones:
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    current_zone = z
                    break
            j = self._open.setdefault(tr.track_id, {
                "zones": [], "started_at": now, "current_zone_id": None,
            })
            cur_id = current_zone["id"] if current_zone else None
            if cur_id != j["current_zone_id"]:
                # Close previous zone entry, open new one.
                if j["zones"]:
                    j["zones"][-1]["exited_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                if current_zone is not None:
                    j["zones"].append({
                        "zone_id":   current_zone["id"],
                        "zone_name": current_zone.get("name"),
                        "entered_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                        "exited_at": None,
                    })
                j["current_zone_id"] = cur_id

        # Flush journeys for tracks that disappeared.
        for tid in list(self._open):
            if tid in active_track_ids:
                continue
            j = self._open.pop(tid)
            if len(j["zones"]) < 2:
                continue
            # Close the last zone.
            j["zones"][-1].setdefault("exited_at",
                datetime.fromtimestamp(now, tz=timezone.utc).isoformat())
            try:
                ctx.db.add(CustomerJourney(
                    store_id=ctx.store_id,
                    camera_id=ctx.camera_id,
                    day=date.today(),
                    track_signature=f"cam{ctx.camera_id}:tr{tid}",
                    zone_sequence_json=j["zones"],
                    started_at=datetime.fromtimestamp(j["started_at"], tz=timezone.utc),
                    ended_at=datetime.fromtimestamp(now, tz=timezone.utc),
                ))
            except Exception:
                ctx.db.rollback()
        return []     # no per-frame alerts; journey rows are the output
