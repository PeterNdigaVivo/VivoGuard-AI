"""Directional entry/exit tripwire — net visitor count per day.

Operator tags a `shape="line"` zone with detection type `entry_exit`.
The line's two points define direction A→B. The detector watches each
person's centroid and the *side* of the line it sits on. When the
side flips between frames the person crossed; the sign of the cross
product `(B-A) × (P-A)` determines whether they moved IN or OUT.

Tracker independence
  The previous implementation required two consecutive centroids on
  the SAME tracker ID. Cameras whose tracker dropped IDs between
  frames (occlusion, low FPS, busy doorway — camera 85 was the
  diagnostic) logged endless "person near line" but never fired
  CROSSING. We now maintain our own position-based pseudo-tracks per
  (camera, zone) — proximity matching across frames means a tracker
  ID gap doesn't blank the previous-side reference. Each pseudo-
  track also carries a refire cooldown so a person hovering on the
  line can't be counted dozens of times.

Conventions (override via detection_configs.extra):
  inward_sign: +1.0  — positive cross product = "in" (default)
  Flip to -1.0 if your line orientation gives the opposite sign for
  customers walking into the store.

Metrics written (per zone):
  visitor_count_in    — sum-aggregated per 1-min bucket
  visitor_count_out   — sum-aggregated per 1-min bucket
The chain dashboard sums (in - out) for a clean net visitor figure.

Diagnostic logging: every branch logs at INFO so the worker log alone
shows whether the detector is being asked to run, whether zones are
present, how many people are near the line, and every crossing fires
unthrottled.
"""
from __future__ import annotations
import logging
import time

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_centre

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 30.0     # per-camera diagnostic log throttle


def _segment_distance(p, a, b) -> float:
    """Shortest distance from point p to segment a–b (normalised
    coords 0..1)."""
    ax, ay = a; bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((p[0] - ax) ** 2 + (p[1] - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0,
                     ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    qx, qy = ax + t * dx, ay + t * dy
    return ((p[0] - qx) ** 2 + (p[1] - qy) ** 2) ** 0.5


def _side(p, a, b) -> int:
    """+1 / 0 / -1 — which side of line A→B point P sits on (sign of
    cross product (B-A) × (P-A))."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    px, py = p[0] - a[0], p[1] - a[1]
    c = dx * py - dy * px
    if c > 0:  return 1
    if c < 0:  return -1
    return 0


def _dist(p, q) -> float:
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


class EntryExitDetector(Detector):
    detection_type = "entry_exit"
    needs_tracking = True

    # Match this frame's person to a pseudo-track when their centroids
    # are within this normalised distance (≈25% of frame width/height).
    # Wide enough to bridge a tracker-ID drop where the person moved a
    # fair distance between frames at 5 FPS, tight enough that two
    # genuinely different people don't collapse into one pseudo-track.
    MATCH_RADIUS = 0.25
    # Drop pseudo-tracks not seen for this long. Spec asks for a
    # 30-second memory window — match it. A person who steps out of
    # frame and returns within 30 s still gets credited.
    PSEUDO_TRACK_TTL = 30.0
    # Cooldown between consecutive crossing fires for the same
    # pseudo-track (so a person hovering on the line is counted once
    # per genuine crossing).
    REFIRE_COOLDOWN = 3.0
    # If the centroid is within this distance of the line treat side
    # as 0 — prevents sub-pixel oscillation registering as a crossing.
    # Kept tight (0.5% of frame) so legitimate near-line crossings
    # still register.
    SIDE_DEADBAND = 0.005
    # Diagnostic log threshold for "person near line".
    NEAR_LINE_THRESHOLD = 0.10

    def __init__(self):
        # (camera_id, zone_id) → list[{cx, cy, side, last_seen, last_fired}]
        self._pseudo: dict[tuple[int, int], list[dict]] = {}
        self._last_hb: dict[int, float] = {}
        self._last_near: dict[tuple[int, int], float] = {}
        # Per-camera flag so the "code path loaded" banner only logs
        # once — confirms the position-based pseudo-track code is
        # what's running, not the retired prev/curr centroid version.
        self._announced: set[int] = set()

    # ---- log throttles -----------------------------------------------

    def _hb_due(self, camera_id: int, now: float) -> bool:
        prev = self._last_hb.get(camera_id, 0.0)
        if now - prev >= _HEARTBEAT_SECONDS:
            self._last_hb[camera_id] = now
            return True
        return False

    def _near_due(self, camera_id: int, idx: int, now: float) -> bool:
        key = (camera_id, idx)
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
                wrong_shape = sum(
                    1 for z in ctx.zones
                    if "entry_exit" in (z.get("detection_types_json") or [])
                    and z.get("shape") != "line")
                log.info("EntryExit camera=%s no entry_exit LINES "
                         "(zones=%d, entry_exit-tagged but wrong shape=%d). "
                         "Draw a 2-point line and tag it entry_exit.",
                         ctx.camera_id, len(ctx.zones), wrong_shape)
            return []

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

        # Use raw_detections — track IDs are unreliable on cameras
        # with occlusion or low FPS, and the pseudo-track below
        # handles continuity itself.
        thr = float(cfg.get("confidence_threshold", 0.5))
        persons = [d for d in ctx.raw_detections
                   if d.get("cls") in COCO_PERSON and d.get("conf", 0.0) >= thr]
        if ctx.camera_id not in self._announced:
            self._announced.add(ctx.camera_id)
            log.info("EntryExit camera=%s pseudo-track impl loaded "
                     "(MATCH_RADIUS=%.2f PSEUDO_TRACK_TTL=%.0fs "
                     "SIDE_DEADBAND=%.3f conf_thr=%.2f) — visitor counting "
                     "and shop-open alerts are independent gates.",
                     ctx.camera_id, self.MATCH_RADIUS, self.PSEUDO_TRACK_TTL,
                     self.SIDE_DEADBAND, thr)
        if self._hb_due(ctx.camera_id, now):
            log.info("EntryExit camera=%s zones=%d persons=%d",
                     ctx.camera_id, len(good_lines), len(persons))

        inward_sign = float(((cfg.get("extra") or {}).get("inward_sign")) or 1.0)
        out: list[DetectionEvent] = []

        for z in good_lines:
            pts = z["polygon_coords_json"]
            a = (pts[0][0], pts[0][1])
            b = (pts[1][0], pts[1][1])
            key = (ctx.camera_id, z["id"])
            hist = self._pseudo.setdefault(key, [])
            # Drop stale entries before matching this frame.
            hist[:] = [e for e in hist if now - e["last_seen"] <= self.PSEUDO_TRACK_TTL]
            matched_indices: set[int] = set()

            for det_idx, det in enumerate(persons):
                cx, cy = bbox_centre(det["bbox_norm"])
                dist_to_line = _segment_distance((cx, cy), a, b)
                if (dist_to_line <= self.NEAR_LINE_THRESHOLD
                        and self._near_due(ctx.camera_id, det_idx, now)):
                    log.info("EntryExit camera=%s person near line zone=%s "
                             "idx=%d dist=%.3f", ctx.camera_id, z["id"],
                             det_idx, dist_to_line)

                # Side with a deadband near the line so sub-pixel
                # jitter on a centroid sitting right on the line
                # doesn't generate phantom crossings.
                side_now = 0 if dist_to_line < self.SIDE_DEADBAND \
                              else _side((cx, cy), a, b)

                # Match to nearest pseudo-track within MATCH_RADIUS,
                # skipping ones already claimed by another detection
                # this frame.
                best_i, best_d = -1, float("inf")
                for i, e in enumerate(hist):
                    if i in matched_indices:
                        continue
                    d = _dist((cx, cy), (e["cx"], e["cy"]))
                    if d < best_d and d <= self.MATCH_RADIUS:
                        best_i, best_d = i, d

                if best_i < 0:
                    # First sighting — no crossing inferable yet.
                    hist.append({"cx": cx, "cy": cy, "side": side_now,
                                 "last_seen": now, "last_fired": 0.0})
                    continue

                entry = hist[best_i]
                matched_indices.add(best_i)
                prev_side = entry["side"]

                # Crossing = side genuinely flipped. Skip when either
                # side is in the deadband — wait for a clean read.
                if (side_now != 0 and prev_side != 0 and side_now != prev_side
                        and now - entry["last_fired"] >= self.REFIRE_COOLDOWN):
                    direction = "in" if (side_now * inward_sign) > 0 else "out"
                    log.info("EntryExit camera=%s CROSSING direction=%s zone=%s "
                             "prev_side=%d new_side=%d prev=(%.3f,%.3f) "
                             "cur=(%.3f,%.3f)",
                             ctx.camera_id, direction, z["id"],
                             prev_side, side_now,
                             entry["cx"], entry["cy"], cx, cy)

                    # Visitor counting runs UNCONDITIONALLY — the time
                    # window gate lives in shop_state.maybe_emit_*_alert
                    # and only suppresses the operator-facing "Store
                    # Opened/Closed" alert, never the metric.
                    if ctx.db is not None:
                        from app.analytics import recorder
                        recorder.record(ctx.db, f"visitor_count_{direction}", 1.0,
                                        camera_id=ctx.camera_id, store_id=ctx.store_id,
                                        zone_id=z["id"], aggregator="sum")

                    out.append(DetectionEvent(
                        detection_type=self.detection_type,
                        cls=f"crossing_{direction}",
                        confidence=1.0, bbox_norm=det["bbox_norm"],
                        zone_id=z["id"],
                        extra={"direction": direction, "store_id": ctx.store_id},
                    ))
                    entry["last_fired"] = now

                    from app.ai.detectors import shop_state
                    shop_alert = None
                    if direction == "in":
                        shop_alert = shop_state.maybe_emit_open_alert(
                            ctx, cfg.get("extra"), 0, z["id"], det["bbox_norm"])
                    else:
                        shop_alert = shop_state.maybe_emit_close_alert(
                            ctx, cfg.get("extra"), 0, z["id"], det["bbox_norm"])
                    if shop_alert is not None:
                        log.info("EntryExit camera=%s shop alert raised: rule=%s",
                                 ctx.camera_id,
                                 (shop_alert.extra or {}).get("rule"))
                        out.append(shop_alert)

                # Always update position + last_seen. Only commit the
                # side when it's NOT in the deadband — otherwise we'd
                # erase the last clean reference.
                entry["cx"] = cx
                entry["cy"] = cy
                if side_now != 0:
                    entry["side"] = side_now
                entry["last_seen"] = now
        return out
