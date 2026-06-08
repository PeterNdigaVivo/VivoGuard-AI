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

Diagnostic logging: every branch logs at INFO so the worker log alone
shows whether the detector is even being asked to run, whether zones
are present, how many people are tracked near the line, and which
direction crossings fired. Per-camera throttle of 30 s keeps the log
volume reasonable at 5–10 FPS.
"""
from __future__ import annotations
import logging
import time

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_centre, segments_cross

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 30.0      # per-camera diagnostic log throttle


def _segment_distance(p: tuple[float, float],
                       a: tuple[float, float],
                       b: tuple[float, float]) -> float:
    """Shortest distance from point p to segment a–b (all in normalised
    image coords 0..1). Used purely for the "person near line" log."""
    ax, ay = a; bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((p[0] - ax) ** 2 + (p[1] - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0,
                     ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    qx, qy = ax + t * dx, ay + t * dy
    return ((p[0] - qx) ** 2 + (p[1] - qy) ** 2) ** 0.5


class EntryExitDetector(Detector):
    detection_type = "entry_exit"
    needs_tracking = True

    # If the closest distance from any tracked person centroid to a
    # line is below this (normalised units), log "person near line".
    # 0.10 ≈ 10% of the frame width/height — broad enough to catch
    # someone clearly walking up to the door.
    NEAR_LINE_THRESHOLD = 0.10

    def __init__(self):
        # (camera, track, zone) → last (cx, cy) in normalised coords
        self._last_centroid: dict[tuple[int, int, int], tuple[float, float]] = {}
        # camera_id → epoch of last heartbeat / near-line / disabled log.
        self._last_hb: dict[int, float] = {}
        self._last_near: dict[tuple[int, int], float] = {}    # (cam, track)

    # ---- log throttles -------------------------------------------------

    def _hb_due(self, camera_id: int, now: float) -> bool:
        prev = self._last_hb.get(camera_id, 0.0)
        if now - prev >= _HEARTBEAT_SECONDS:
            self._last_hb[camera_id] = now
            return True
        return False

    def _near_due(self, camera_id: int, track_id: int, now: float) -> bool:
        key = (camera_id, track_id)
        prev = self._last_near.get(key, 0.0)
        if now - prev >= _HEARTBEAT_SECONDS:
            self._last_near[key] = now
            return True
        return False

    # ---- main --------------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        now = time.time()
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            if self._hb_due(ctx.camera_id, now):
                log.info("EntryExit camera=%s DISABLED (detector config missing or enabled=False)",
                         ctx.camera_id)
            return []

        lines = [z for z in ctx.zones
                 if z.get("shape") == "line"
                 and "entry_exit" in (z.get("detection_types_json") or [])]
        if not lines:
            if self._hb_due(ctx.camera_id, now):
                # Why no zones — operator drew a polygon-shape zone
                # tagged entry_exit, or no entry_exit-tagged zone at
                # all. Both are common misconfigurations.
                wrong_shape = sum(
                    1 for z in ctx.zones
                    if "entry_exit" in (z.get("detection_types_json") or [])
                    and z.get("shape") != "line")
                log.info("EntryExit camera=%s no entry_exit LINES "
                         "(zones=%d, entry_exit-tagged but wrong shape=%d). "
                         "Draw a 2-point line and tag it entry_exit.",
                         ctx.camera_id, len(ctx.zones), wrong_shape)
            return []

        # Validate each line up-front so the loop can skip bad ones
        # and log a clear reason once.
        good_lines = []
        for z in lines:
            pts = z.get("polygon_coords_json")
            if not pts or len(pts) < 2:
                if self._hb_due(ctx.camera_id, now):
                    log.warning("EntryExit camera=%s zone=%s INVALID — "
                                "shape=line but polygon_coords_json has %d "
                                "points (need ≥2).", ctx.camera_id,
                                z.get("id"), len(pts or []))
                continue
            good_lines.append(z)
        if not good_lines:
            return []

        persons = [tr for tr, _det in ctx.tracks if tr.cls in COCO_PERSON]
        if self._hb_due(ctx.camera_id, now):
            log.info("EntryExit camera=%s zones=%d persons=%d",
                     ctx.camera_id, len(good_lines), len(persons))

        inward_sign = float(((cfg.get("extra") or {}).get("inward_sign")) or 1.0)
        out: list[DetectionEvent] = []

        for tr in persons:
            cx, cy = bbox_centre(tr.bbox_norm)
            for z in good_lines:
                pts = z["polygon_coords_json"]
                ax, ay = pts[0]
                bx, by = pts[1]

                # Diagnostic: if the person's centroid is near the line
                # but not crossing it, log occasionally so the operator
                # can tell tracking + zone placement are wired up even
                # before the first crossing fires.
                dist = _segment_distance((cx, cy), (ax, ay), (bx, by))
                if (dist <= self.NEAR_LINE_THRESHOLD
                        and self._near_due(ctx.camera_id, tr.track_id, now)):
                    log.info("EntryExit camera=%s person near line zone=%s "
                             "track=%s dist=%.3f", ctx.camera_id, z["id"],
                             tr.track_id, dist)

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

                # ALWAYS log a real crossing — never throttle this; it's
                # the signal everything downstream depends on.
                log.info("EntryExit camera=%s CROSSING direction=%s zone=%s "
                         "track=%s prev=(%.3f,%.3f) cur=(%.3f,%.3f)",
                         ctx.camera_id, direction, z["id"], tr.track_id,
                         prev[0], prev[1], cx, cy)

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
                # entry_exit line) ever reach this branch.
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
                    log.info("EntryExit camera=%s shop alert raised: rule=%s",
                             ctx.camera_id, (shop_alert.extra or {}).get("rule"))
                    out.append(shop_alert)
        return out
