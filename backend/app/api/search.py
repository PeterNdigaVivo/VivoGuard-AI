"""Natural-language event search.

POST /search {"q": "show me all people near the fitting rooms after 6pm"}

Heuristic parser — no LLM dependency. Recognises:
  • Detection types from DETECTION_TYPES (people=person, cars=vehicle, …)
  • Store names from the stores table  ("at Sarit", "in Junction")
  • Zone names mentioned by substring ("near the fitting rooms")
  • Time phrases: "today", "yesterday", "this week", "after 6pm",
                  "between 9am and noon", "last 7 days", "last hour"

Returns: list of DetectionEvent + joined camera/zone names, plus the
parsed filter so the operator can see what was inferred and refine.
"""
from __future__ import annotations
import re
from datetime import date, datetime, time as time_t, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Camera, DetectionEvent, Store, Zone, DETECTION_TYPES

router = APIRouter(prefix="/search", tags=["search"])


# --- aliases used in colloquial phrases -----------------------------
TYPE_ALIASES = {
    "people":   "person",  "person":  "person", "customer": "person", "staff": "person",
    "vehicle":  "vehicle", "vehicles":"vehicle", "car": "vehicle", "cars": "vehicle",
    "weapon":   "weapon",  "weapons": "weapon",  "gun": "weapon", "knife": "weapon",
    "fire":     "fire",    "smoke":   "smoke",
    "queue":    "queue",   "queues":  "queue",
    "fight":    "fight",   "fighting": "fight",  "altercation": "fight",
    "intruder": "intrusion","intrusion": "intrusion",
    "fall":     "fall",    "fallen":  "fall",
    "loiter":   "loitering","loitering": "loitering",
    "shutter":  "shutter",
    "shrinkage":"shrinkage","theft":  "shrinkage",
    "uniform":  "uniform_compliance",
    "plate":    "lpr",     "lpr":     "lpr",     "license": "lpr",
}


class SearchIn(BaseModel):
    q: str
    limit: int = 200


def _parse(q: str, db: Session) -> dict[str, Any]:
    """Return a filter dict with keys: detection_type, store_id, zone_id,
    since, until, plain (text that didn't match anything)."""
    text = q.lower().strip()
    out: dict[str, Any] = {"detection_type": None, "store_id": None,
                           "zone_id": None, "since": None, "until": None,
                           "raw": q, "matched": []}

    # Detection type — first alias hit wins.
    for word, dt in TYPE_ALIASES.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            out["detection_type"] = dt
            out["matched"].append(f"type={dt}")
            break
    # Fallback: any literal DETECTION_TYPES string.
    if not out["detection_type"]:
        for dt in DETECTION_TYPES:
            if dt in text:
                out["detection_type"] = dt
                out["matched"].append(f"type={dt}")
                break

    # Store name — substring match against stores.
    for s in db.query(Store).all():
        if s.name and s.name.lower() in text:
            out["store_id"] = s.id
            out["matched"].append(f"store={s.name}")
            break

    # Zone name — substring match against zones (e.g. "fitting rooms").
    if out["store_id"]:
        q_zones = (db.query(Zone)
                     .join(Camera, Camera.id == Zone.camera_id)
                     .filter(Camera.store_id == out["store_id"]).all())
    else:
        q_zones = db.query(Zone).all()
    for z in q_zones:
        if z.name and z.name.lower() in text:
            out["zone_id"] = z.id
            out["matched"].append(f"zone={z.name}")
            break

    # Time phrases.
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(date.today(), time_t.min, tzinfo=timezone.utc)
    if "today" in text:
        out["since"] = today_start
    elif "yesterday" in text:
        out["since"] = today_start - timedelta(days=1)
        out["until"] = today_start
    elif "this week" in text:
        out["since"] = today_start - timedelta(days=date.today().weekday())
    elif m := re.search(r"last\s+(\d+)\s+(hour|day|week)s?", text):
        n = int(m.group(1)); unit = m.group(2)
        delta = {"hour": timedelta(hours=n), "day": timedelta(days=n),
                 "week": timedelta(weeks=n)}[unit]
        out["since"] = now - delta
    if m := re.search(r"after\s+(\d{1,2})\s*(am|pm)?", text):
        hr = int(m.group(1))
        if m.group(2) == "pm" and hr < 12:
            hr += 12
        if m.group(2) == "am" and hr == 12:
            hr = 0
        anchor = today_start + timedelta(hours=hr)
        out["since"] = max(out["since"], anchor) if out["since"] else anchor
    if m := re.search(r"before\s+(\d{1,2})\s*(am|pm)?", text):
        hr = int(m.group(1))
        if m.group(2) == "pm" and hr < 12:
            hr += 12
        out["until"] = today_start + timedelta(hours=hr)

    return out


@router.post("")
def search(body: SearchIn, db: Session = Depends(get_db), _u=Depends(get_current_user)):
    f = _parse(body.q, db)
    q = (db.query(DetectionEvent, Camera, Zone)
           .join(Camera, Camera.id == DetectionEvent.camera_id)
           .outerjoin(Zone, Zone.id == DetectionEvent.zone_id))
    if f["detection_type"]:
        q = q.filter(DetectionEvent.detection_type == f["detection_type"])
    if f["store_id"]:
        q = q.filter(Camera.store_id == f["store_id"])
    if f["zone_id"]:
        q = q.filter(DetectionEvent.zone_id == f["zone_id"])
    if f["since"]:
        q = q.filter(DetectionEvent.timestamp >= f["since"])
    if f["until"]:
        q = q.filter(DetectionEvent.timestamp <= f["until"])
    rows = q.order_by(desc(DetectionEvent.timestamp)).limit(body.limit).all()
    return {
        "parsed_filter": {k: v for k, v in f.items()
                          if k not in ("raw",) and v is not None},
        "matched_terms": f["matched"],
        "results": [
            {
                "id": ev.id, "timestamp": ev.timestamp.isoformat(),
                "detection_type": ev.detection_type, "confidence": ev.confidence,
                "bbox_norm": ev.bbox_json, "camera_id": cam.id,
                "camera_name": cam.name, "store_id": cam.store_id,
                "zone_id": z.id if z else None,
                "zone_name": z.name if z else None,
                "thumbnail_path": ev.thumbnail_path,
                "extra": ev.extra,
            }
            for ev, cam, z in rows
        ],
        "result_count": len(rows),
    }
