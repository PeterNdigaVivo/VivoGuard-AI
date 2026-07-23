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
from app.config import settings          # used by evaluate() (P5 gate)

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

    # P5: a detection gap longer than this while a crossing is pending is
    # treated as a doorway occlusion — the strict "N frames on the new side"
    # rule is relaxed to "≥1 frame on the new side" (see Q3 exception).
    OCCLUSION_GAP_SECONDS = 1.0
    # If the centroid is within this distance of the line treat side
    # as 0 — prevents sub-pixel oscillation registering as a crossing.
    # Kept tight (0.5% of frame) so legitimate near-line crossings
    # still register.
    SIDE_DEADBAND = 0.005
    # Diagnostic log threshold for "person near line".
    NEAR_LINE_THRESHOLD = 0.10

    # ---- glass-door modifier ----------------------------------------
    # Zones tagged with `glass_door` (in addition to `entry_exit`) get
    # stricter filtering — glass reflections produce low-confidence
    # detections that flicker for a frame or two. The stricter
    # threshold + multi-frame persistence requirement cuts the
    # false-crossing rate on these cameras.
    GLASS_DOOR_MIN_CONF = 0.65
    # Default kept at 1 because at the platform's 1-2 fps inference
    # rate a real person passes through the door in a single frame;
    # requiring >1 frames_seen blocks every legitimate crossing while
    # the 0.65 confidence threshold already filters reflections.
    # Env-overridable via GLASS_DOOR_MIN_FRAMES (resolved at evaluate
    # time so a config change picks up without restarting the worker).
    GLASS_DOOR_MIN_FRAMES_SEEN_DEFAULT = 1

    @classmethod
    def _glass_door_min_frames(cls) -> int:
        try:
            from app.config import settings
            return max(1, int(getattr(settings, "glass_door_min_frames",
                                       cls.GLASS_DOOR_MIN_FRAMES_SEEN_DEFAULT)))
        except Exception:
            return cls.GLASS_DOOR_MIN_FRAMES_SEEN_DEFAULT

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

            # Per-zone glass-door modifier: stricter conf + multi-frame
            # persistence. Plain `entry_exit` zones use the unmodified
            # pre-filtered `persons` list and fire on the first clean
            # side-flip as before.
            is_glass_door = "glass_door" in (z.get("detection_types_json") or [])
            zone_persons = (
                [d for d in persons if d.get("conf", 0.0) >= self.GLASS_DOOR_MIN_CONF]
                if is_glass_door else persons
            )

            for det_idx, det in enumerate(zone_persons):
                # Centroid drives pseudo-track matching + stored position
                # below — leave it untouched so track continuity is
                # unaffected.
                cx, cy = bbox_centre(det["bbox_norm"])
                # Foot point (bottom-centre of the bbox) drives the SIDE
                # test ONLY. On angled/high entrance cameras the box
                # centroid can stay on one side of the line even as the
                # person physically crosses (their feet cross first), so
                # the side sign never flips and no crossing fires. The
                # foot point crosses the entrance line reliably.
                # bbox_norm is [x1, y1, x2, y2]; y increases downward, so
                # y2 (the 4th value) is the bottom of the box = the feet.
                bx1, by1, bx2, by2 = det["bbox_norm"]
                fx, fy = (bx1 + bx2) / 2.0, by2
                dist_to_line = _segment_distance((fx, fy), a, b)
                if (dist_to_line <= self.NEAR_LINE_THRESHOLD
                        and self._near_due(ctx.camera_id, det_idx, now)):
                    log.info("EntryExit camera=%s person near line zone=%s "
                             "idx=%d dist=%.3f", ctx.camera_id, z["id"],
                             det_idx, dist_to_line)

                # Side with a deadband near the line so sub-pixel
                # jitter on a foot point sitting right on the line
                # doesn't generate phantom crossings.
                side_now = 0 if dist_to_line < self.SIDE_DEADBAND \
                              else _side((fx, fy), a, b)

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
                                 "last_seen": now, "last_fired": 0.0,
                                 "frames_seen": 1})
                    continue

                entry = hist[best_i]
                matched_indices.add(best_i)
                # Bump the consecutive-match counter — the glass-door
                # branch below uses this to suppress single-frame
                # reflection flickers.
                entry["frames_seen"] = int(entry.get("frames_seen", 1)) + 1
                prev_side = entry["side"]

                # Crossing = side genuinely flipped. Skip when either
                # side is in the deadband — wait for a clean read.
                # For glass-door zones, ALSO require the pseudo-track
                # to have been matched on ≥ settings.glass_door_min_frames
                # frames so a reflection that appears for a single
                # frame can't trigger a crossing. Default 1 — see
                # GLASS_DOOR_MIN_FRAMES_SEEN_DEFAULT comment above.
                glass_min_frames = self._glass_door_min_frames() if is_glass_door else 1
                glass_persistence_ok = (
                    (not is_glass_door)
                    or entry["frames_seen"] >= glass_min_frames
                )
                min_side = int(getattr(settings, "entry_exit_min_frames_per_side", 3))
                frame_gap = now - float(entry.get("last_seen", now))

                # P5 (strict both-sides): a side-flip only OPENS a pending
                # crossing once the person has dwelt >= min_side frames on the
                # side they came FROM. It is NOT counted until the NEW side is
                # confirmed below — kills 1-frame boundary-flicker double-counts.
                if (side_now != 0 and prev_side != 0 and side_now != prev_side
                        and now - entry["last_fired"] >= self.REFIRE_COOLDOWN
                        and glass_persistence_ok
                        and int(entry.get("side_frames", 0)) >= min_side):
                    entry["pending"] = {
                        "direction": "in" if (side_now * inward_sign) > 0 else "out",
                        "to_side":   side_now, "since": now, "new_frames": 0,
                    }

                # Confirm + emit a pending crossing once on the NEW side:
                #   • strict:    >= min_side frames on the new side, OR
                #   • occlusion: the person was solidly on the prior side
                #     (pending <= 10s old), reappeared after a detection gap,
                #     and shows >= 1 frame on the new side (Q3 exception).
                pend = entry.get("pending")
                if pend is not None and side_now != 0 and side_now == pend.get("to_side"):
                    pend["new_frames"] = int(pend.get("new_frames", 0)) + 1
                    strict_ok = pend["new_frames"] >= min_side
                    occlusion_ok = (frame_gap > self.OCCLUSION_GAP_SECONDS
                                    and (now - pend["since"]) <= 10.0
                                    and pend["new_frames"] >= 1)
                    if strict_ok or occlusion_ok:
                        direction = pend["direction"]
                        entry.pop("pending", None)
                        log.info("EntryExit camera=%s CROSSING direction=%s zone=%s "
                                 "mode=%s new_frames=%d gap=%.1fs",
                                 ctx.camera_id, direction, z["id"],
                                 "strict" if strict_ok else "occlusion",
                                 pend["new_frames"], frame_gap)
                        # Visitor counting is unconditional — the trading-window
                        # gate lives in shop_state and only suppresses the
                        # operator-facing Store Opened/Closed alert, not metrics.
                        if ctx.db is not None:
                            from app.analytics import recorder
                            recorder.record(ctx.db, f"visitor_count_{direction}", 1.0,
                                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                                            zone_id=z["id"],
                                            dims={"source": ("glass_door" if is_glass_door
                                                              else "entry_exit")},
                                            aggregator="sum")
                        out.append(DetectionEvent(
                            detection_type=self.detection_type,
                            cls=f"crossing_{direction}",
                            confidence=1.0, bbox_norm=det["bbox_norm"],
                            zone_id=z["id"],
                            extra={"direction": direction, "store_id": ctx.store_id},
                        ))
                        entry["last_fired"] = now
                        from app.ai.detectors import shop_state
                        if direction == "in":
                            shop_alert = shop_state.maybe_emit_open_alert(
                                ctx, cfg.get("extra"), 0, z["id"], det["bbox_norm"],
                                via_glass_door=is_glass_door)
                        else:
                            shop_alert = shop_state.maybe_emit_close_alert(
                                ctx, cfg.get("extra"), 0, z["id"], det["bbox_norm"],
                                via_glass_door=is_glass_door)
                        if shop_alert is not None:
                            log.info("EntryExit camera=%s shop alert raised: rule=%s",
                                     ctx.camera_id, (shop_alert.extra or {}).get("rule"))
                            out.append(shop_alert)

                # Always update position + last_seen. Only commit the
                # side when it's NOT in the deadband — otherwise we'd
                # erase the last clean reference.
                entry["cx"] = cx
                entry["cy"] = cy
                if side_now != 0:
                    # P5: count consecutive frames on the committed side so the
                    # crossing gate can require the person to have genuinely
                    # dwelt on a side (>=3 frames) rather than a 1-frame jitter.
                    if side_now == entry.get("side"):
                        entry["side_frames"] = int(entry.get("side_frames", 0)) + 1
                    else:
                        entry["side_frames"] = 1
                    entry["side"] = side_now
                entry["last_seen"] = now
        return out
