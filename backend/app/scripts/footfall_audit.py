"""Footfall health audit — why a store counts nothing, or counts wrongly.

Run:  docker compose exec -T api python -m app.scripts.footfall_audit
      docker compose exec -T api python -m app.scripts.footfall_audit --json
      docker compose exec -T api python -m app.scripts.footfall_audit --days 14

Footfall is the foundation metric: conversion is sales / footfall, and
heatmap merchandising analytics lean on the same person tracks. Before
any of that is worth building, each store has to be in one of two
states — counting correctly, or visibly flagged as not counting. The
failure mode we are trying to kill is the third state: a store quietly
reporting a number that is wrong.

The audit sorts every store into exactly one verdict and names the next
action. It is read-only and safe to run any time.

Verdicts, worst first:
  NO_ENTRY_ZONE     no entry_exit zone exists at all
  NO_LINE_ZONE      zones exist but none is shape="line". EntryExitDetector
                    needs two endpoints to compute a side-flip, so a rect
                    or polygon tagged entry_exit counts nothing, silently.
  BULK_DEFAULT      the only entry zones are the bottom-strip template
                    [[0,0.8],[1,0.8],[1,1],[0,1]] applied in bulk to every
                    camera. Not a surveyed doorway — nobody looked at the
                    view. Converting it to a line would produce a number
                    rather than a measurement, which is worse than zero.
  CAMERA_DOWN       a line zone is configured but its camera is offline or
                    pending, so no frames arrive.
  NET_NEGATIVE      counting, but more exits than entries over the window.
                    Physically impossible, so the line's direction
                    (inward_sign) is inverted on at least one zone, or two
                    cameras are double-counting the same doorway.
  IMPLAUSIBLE_LOW   counting, net positive, but below MIN_PLAUSIBLE_PER_DAY
                    entries/day. A fashion store doing single-digit daily
                    footfall is a measurement failure, not a quiet week.
  COUNTING          plausible. NOT the same as validated: no store is
                    "accurate" until it has been checked against a manual
                    count from recorded video.

Deliberately NOT inferred: whether a given camera is the right camera to
count on. Footfall means entries, so exactly one entrance camera per
store should carry the line — if sales-floor cameras also count, the same
shopper is counted several times. That needs a human looking at the view.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from sqlalchemy import text

from app.database import SessionLocal

# The bulk-applied template: bottom 20% strip, full frame width. Any
# entry zone with these exact coordinates was machine-generated, not
# placed by someone looking at the doorway.
BULK_DEFAULT_COORDS = [[0.0, 0.8], [1.0, 0.8], [1.0, 1.0], [0.0, 1.0]]

# Below this, a counting store is not having a quiet week — it is broken.
# Set from the fleet's own floor: the healthiest stores run ~80/day and
# even that is low for mall-front fashion retail, so anything under 20 is
# unambiguously a failure rather than a seasonal dip.
MIN_PLAUSIBLE_PER_DAY = 20.0

_VERDICT_ORDER = [
    "NO_ENTRY_ZONE", "NO_LINE_ZONE", "BULK_DEFAULT", "CAMERA_DOWN",
    "NET_NEGATIVE", "IMPLAUSIBLE_LOW", "COUNTING",
]

_NEXT_ACTION = {
    "NO_ENTRY_ZONE":   "Draw an entry_exit LINE across the doorway on the entrance camera.",
    "NO_LINE_ZONE":    "Redraw the entry_exit zone as shape=line across the doorway.",
    "BULK_DEFAULT":    "Survey the entrance camera view, then place a real line. Do not convert the template.",
    "CAMERA_DOWN":     "Bring the entrance camera back online (NVR / network), then re-check.",
    "NET_NEGATIVE":    "Inverted line direction or double count: check inward_sign, and that only ONE camera carries the line.",
    "IMPLAUSIBLE_LOW": "Line is probably not across the walked path, or the camera does not see the entrance.",
    "COUNTING":        "Validate against a manual count from recorded video before trusting it.",
}


def _coords(raw) -> list | None:
    """polygon_coords_json is double-encoded on some rows (a JSON string
    holding JSON), so decode until we get a list or give up."""
    for _ in range(3):
        if raw is None:
            return None
        if isinstance(raw, list):
            return raw
        if isinstance(raw, (str, bytes)):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return None
        else:
            return None
    return raw if isinstance(raw, list) else None


def audit(days: int = 7) -> list[dict]:
    db = SessionLocal()
    try:
        zone_rows = db.execute(text("""
            SELECT s.id AS store_id, s.name AS store, c.id AS camera_id,
                   c.name AS camera, c.status, c.ai_enabled, c.inference_fps,
                   z.id AS zone_id, z.name AS zone, z.shape, z.suppressed,
                   z.polygon_coords_json AS coords
            FROM stores s
            JOIN cameras c ON c.store_id = s.id AND NOT c.is_deleted
            LEFT JOIN zones z
                   ON z.camera_id = c.id
                  AND z.detection_types_json::text ILIKE '%%entry_exit%%'
        """)).mappings().all()

        count_rows = db.execute(text("""
            SELECT s.id AS store_id,
                   SUM(CASE WHEN m.metric_type='visitor_count_in'  THEN m.value ELSE 0 END) AS cnt_in,
                   SUM(CASE WHEN m.metric_type='visitor_count_out' THEN m.value ELSE 0 END) AS cnt_out
            FROM metric_snapshots m
            JOIN stores s ON s.id = m.store_id
            WHERE m.metric_type IN ('visitor_count_in','visitor_count_out')
              AND m.period_start >= now() - (:days || ' days')::interval
            GROUP BY s.id
        """), {"days": days}).mappings().all()
    finally:
        db.close()

    counts = {r["store_id"]: (float(r["cnt_in"] or 0), float(r["cnt_out"] or 0))
              for r in count_rows}

    stores: dict[int, dict] = {}
    for r in zone_rows:
        st = stores.setdefault(r["store_id"], {
            "store": r["store"], "store_id": r["store_id"],
            "cameras": set(), "line_zones": [], "nonline_zones": [],
            "bulk_default_zones": 0,
        })
        st["cameras"].add(r["camera_id"])
        if r["zone_id"] is None:
            continue
        entry = {
            "zone_id": r["zone_id"], "zone": r["zone"], "shape": r["shape"],
            "camera_id": r["camera_id"], "camera": r["camera"],
            "status": r["status"], "suppressed": bool(r["suppressed"]),
        }
        if (r["shape"] or "").lower() == "line":
            st["line_zones"].append(entry)
        else:
            st["nonline_zones"].append(entry)
            if _coords(r["coords"]) == BULK_DEFAULT_COORDS:
                st["bulk_default_zones"] += 1

    out: list[dict] = []
    for st in stores.values():
        cnt_in, cnt_out = counts.get(st["store_id"], (0.0, 0.0))
        net = cnt_in - cnt_out
        per_day = net / days if days else 0.0

        live_lines = [z for z in st["line_zones"]
                      if (z["status"] or "").lower() == "online"
                      and not z["suppressed"]]

        if not st["line_zones"] and not st["nonline_zones"]:
            verdict = "NO_ENTRY_ZONE"
        elif not st["line_zones"]:
            # Distinguish "someone drew the wrong shape" from "a script
            # stamped the same rectangle on every camera" — they need
            # different fixes, and only one of them is anyone's mistake.
            verdict = ("BULK_DEFAULT"
                       if st["bulk_default_zones"] == len(st["nonline_zones"])
                       else "NO_LINE_ZONE")
        elif not live_lines:
            verdict = "CAMERA_DOWN"
        elif net < 0:
            verdict = "NET_NEGATIVE"
        elif per_day < MIN_PLAUSIBLE_PER_DAY:
            verdict = "IMPLAUSIBLE_LOW"
        else:
            verdict = "COUNTING"

        out.append({
            "store": st["store"],
            "verdict": verdict,
            "cameras": len(st["cameras"]),
            "line_zones": len(st["line_zones"]),
            "live_line_zones": len(live_lines),
            "nonline_zones": len(st["nonline_zones"]),
            "bulk_default_zones": st["bulk_default_zones"],
            "count_in": round(cnt_in),
            "count_out": round(cnt_out),
            "net": round(net),
            "net_per_day": round(per_day, 1),
            "offline_line_cameras": sorted(
                {f'{z["camera_id"]}:{z["status"]}' for z in st["line_zones"]
                 if (z["status"] or "").lower() != "online"}),
            "next_action": _NEXT_ACTION[verdict],
        })

    out.sort(key=lambda r: (_VERDICT_ORDER.index(r["verdict"]), r["store"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="window for the visitor counts (default 7)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    rows = audit(days=args.days)
    if args.json:
        print(json.dumps(rows, indent=2))
        return

    by_verdict: dict[str, int] = defaultdict(int)
    for r in rows:
        by_verdict[r["verdict"]] += 1

    print(f"\nFOOTFALL AUDIT — {len(rows)} stores, {args.days}-day window\n")
    print(f'{"STORE":<20} {"VERDICT":<16} {"CAMS":>4} {"LINE":>4} '
          f'{"IN":>6} {"OUT":>6} {"NET":>6} {"/DAY":>6}')
    print("-" * 78)
    for r in rows:
        print(f'{r["store"][:20]:<20} {r["verdict"]:<16} {r["cameras"]:>4} '
              f'{r["live_line_zones"]:>4} {r["count_in"]:>6} {r["count_out"]:>6} '
              f'{r["net"]:>6} {r["net_per_day"]:>6}')

    print("\nSUMMARY")
    for v in _VERDICT_ORDER:
        if by_verdict.get(v):
            print(f"  {v:<16} {by_verdict[v]:>3}   {_NEXT_ACTION[v]}")

    counting = by_verdict.get("COUNTING", 0)
    print(f"\n{counting} of {len(rows)} stores produce a plausible number. "
          f"None is validated against a manual count yet, so none should be "
          f"presented as fact.\n")


if __name__ == "__main__":
    main()
