"""Directional entry/exit tripwire — net visitor count per day.

Operator tags a `shape="line"` zone with detection type `entry_exit`.
The line's two points define direction A→B. The detector tracks each
person's previous and current centroid; when a person crosses the line,
the sign of `(B-A) × (now-prev)` tells us which direction they moved.

Conventions (override via detection_configs.extra):
  inward_sign: +1.0  — positive cross product = "in" (default)
  Flip to -1.0 if your line orientation gives the opposite sign for
  customers walking into the store.

Metrics written (per zone):
  visitor_count_in    — sum-aggregated per 1-min bucket
  visitor_count_out   — sum-aggregated per 1-min bucket
The chain dashboard sums (in - out) for a clean net visitor figure.
"""
from __future__ import annotations

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_centre, segments_cross


class EntryExitDetector(Detector):
    detection_type = "entry_exit"
    needs_tracking = True

    def __init__(self):
        # (camera, track, zone) → last (cx, cy) in normalised coords
        self._last_centroid: dict[tuple[int, int, int], tuple[float, float]] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        lines = [z for z in ctx.zones
                 if z.get("shape") == "line"
                 and "entry_exit" in (z.get("detection_types_json") or [])]
        if not lines:
            return []

        inward_sign = float(((cfg.get("extra") or {}).get("inward_sign")) or 1.0)
        out: list[DetectionEvent] = []

        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            cx, cy = bbox_centre(tr.bbox_norm)
            for z in lines:
                pts = z["polygon_coords_json"]
                if not pts or len(pts) < 2:
                    continue
                ax, ay = pts[0]
                bx, by = pts[1]
                key = (ctx.camera_id, tr.track_id, z["id"])
                prev = self._last_centroid.get(key)
                self._last_centroid[key] = (cx, cy)
                if prev is None:
                    continue
                # Did the centroid segment cross the line segment?
                if not segments_cross(prev, (cx, cy), (ax, ay), (bx, by)):
                    continue
                # Direction via cross product.
                line_dx, line_dy = bx - ax, by - ay
                move_dx, move_dy = cx - prev[0], cy - prev[1]
                cross = line_dx * move_dy - line_dy * move_dx
                direction = "in" if (cross * inward_sign) > 0 else "out"

                if ctx.db is not None:
                    from app.analytics import recorder
                    recorder.record(ctx.db, f"visitor_count_{direction}", 1.0,
                                    camera_id=ctx.camera_id, store_id=ctx.store_id,
                                    zone_id=z["id"], aggregator="sum")

                out.append(DetectionEvent(
                    detection_type=self.detection_type,
                    cls=f"crossing_{direction}",
                    confidence=1.0, bbox_norm=tr.bbox_norm,
                    track_id=tr.track_id, zone_id=z["id"],
                    extra={"direction": direction, "store_id": ctx.store_id},
                ))

                # Shop-open / shop-close alert dispatcher. Reuses this
                # entrance line — only entrance cameras (with an
                # entry_exit line) ever reach this branch. The
                # dispatcher gates open alerts on the latest committed
                # shutter state (signal-agreement rule).
                from app.ai.detectors import shop_state
                shop_alert = None
                if direction == "in":
                    shop_alert = shop_state.maybe_emit_open_alert(
                        ctx, cfg.get("extra"),
                        tr.track_id, z["id"], tr.bbox_norm)
                elif direction == "out":
                    shop_alert = shop_state.maybe_emit_close_alert(
                        ctx, cfg.get("extra"),
                        tr.track_id, z["id"], tr.bbox_norm)
                if shop_alert is not None:
                    out.append(shop_alert)
        return out
