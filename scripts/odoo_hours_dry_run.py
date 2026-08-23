#!/usr/bin/env python3
"""Compare current vs governed-hours classification without changing data."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.database import SessionLocal
from app.models import Camera, DetectionEvent, Store
from app.operations.odoo_assurance import effective_business_hours
from app.utils.business_hours import is_open_with_default, normalise_business_hours


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(args.days, 31)))
    report = {"days": args.days, "events_checked": 0, "classification_changes": 0,
              "would_stop_after_hours": 0, "would_add_after_hours": 0,
              "stores": {}}
    with SessionLocal() as db:
        stores = {store.id: store for store in db.query(Store).all()}
        rows = (db.query(DetectionEvent, Camera)
                .join(Camera, Camera.id == DetectionEvent.camera_id)
                .filter(DetectionEvent.timestamp >= cutoff,
                        DetectionEvent.detection_type.in_(("intrusion", "shop_open_close"))).all())
        for event, camera in rows:
            store = stores.get(camera.store_id)
            if store is None:
                continue
            at = event.timestamp.replace(tzinfo=timezone.utc) if event.timestamp.tzinfo is None else event.timestamp
            local = at.astimezone(ZoneInfo(store.timezone or "Africa/Nairobi"))
            current = is_open_with_default(normalise_business_hours(store.business_hours_json), local)
            governed, source = effective_business_hours(db, store, at)
            proposed = is_open_with_default(governed, local)
            item = report["stores"].setdefault(str(store.id), {
                "store_name": store.name, "source": source, "checked": 0, "changes": 0})
            item["checked"] += 1
            report["events_checked"] += 1
            if current != proposed:
                item["changes"] += 1
                report["classification_changes"] += 1
                if not current and proposed:
                    report["would_stop_after_hours"] += 1
                elif current and not proposed:
                    report["would_add_after_hours"] += 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
