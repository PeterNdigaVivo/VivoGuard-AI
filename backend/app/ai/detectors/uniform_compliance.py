"""Uniform compliance detector for Vivo Fashion Group staff.

Two modes, model-first then rule-based fallback:

1. Custom-model mode: if the assigned model emits class labels
   `uniform_ok` / `uniform_violation` / `no_lanyard` / `civilian`,
   the highest-confidence detection per person wins.

2. Rule-based mode (works immediately, no training): for each person
   standing in a `counter` / `staff` zone, analyse the upper-body
   crop in HSV —
     • correct uniform colour: Vivo black OR burgundy/maroon top
     • lanyard: orange or dark vertical strap across the chest
     • name tag: bright white rectangle on the chest
   and combine into a compliance score:
        0.50 * correct_colour + 0.30 * has_lanyard + 0.20 * has_nametag
     score >= 0.80          → uniform_ok
     0.50 <= score < 0.80   → no_lanyard (partial — usually a missing tag)
     score < 0.50           → uniform_violation

People in non-staff zones are treated as civilians and skipped — we
only score staff who should be in uniform.

Writes the `uniform_compliance_pct` metric (rolling avg, 1.0 ok /
0.5 partial / 0.0 violation). Alerts:
  • violation sustained > 2 min  → "staff uniform violation" (warning)
  • no lanyard sustained > 5 min → "staff missing name tag"  (info)
  • > 3 violations in a day      → "repeated uniform violations" (warning)
Dedup: same track + same violation, max once per 30 min.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone, iou


# Compliance state constants — also the alert `cls` strings.
OK, NO_LANYARD, VIOLATION, CIVILIAN = (
    "uniform_ok", "no_lanyard", "uniform_violation", "civilian")
_METRIC = {OK: 1.0, NO_LANYARD: 0.5, VIOLATION: 0.0}

# Sustained-duration thresholds (seconds) before an alert fires.
VIOLATION_SECONDS = 2 * 60
NO_LANYARD_SECONDS = 5 * 60
# Per (track, kind) dedup window.
DEDUP_SECONDS = 30 * 60
# Repeated-violations-today threshold.
REPEAT_THRESHOLD = 3

STAFF_ZONE_TAGS = {"counter", "staff"}


def uniform_colour_score(frame_bgr, bbox_norm) -> float | None:
    """Standalone Vivo-uniform colour score (0..1) for one person bbox.
    Shared by the StaffPresenceDetector to tell staff from customers at
    the counter. Returns None when pixels/cv2 aren't available.

    Score = 0.50*correct_colour + 0.30*has_lanyard + 0.20*has_nametag,
    where correct_colour matches Vivo BLACK or BURGUNDY tops."""
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0 or not bbox_norm:
        return None
    try:
        h, w = frame_bgr.shape[:2]
        x1, y1, x2, y2 = bbox_norm
        px1, py1 = int(max(0, x1) * w), int(max(0, y1) * h)
        px2, py2 = int(min(1, x2) * w), int(min(1, y2) * h)
        if px2 <= px1 or py2 <= py1:
            return None
        bh = py2 - py1
        ub = frame_bgr[py1:py1 + int(bh * 0.40), px1:px2]
        if ub.size == 0:
            return None
        hsv = cv2.cvtColor(ub, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        total = H.size or 1
        black = ((S < 60) & (V < 70)).sum() / total
        red_h = ((H <= 10) | (H >= 160))
        burgundy = (red_h & (S > 100) & (V > 40) & (V < 170)).sum() / total
        correct_colour = 1.0 if max(black, burgundy) >= 0.35 else \
                         (0.5 if max(black, burgundy) >= 0.20 else 0.0)
        cy1 = py1 + int(bh * 0.30); cy2 = py1 + int(bh * 0.60)
        cw = px2 - px1
        cx1 = px1 + int(cw * 0.30); cx2 = px1 + int(cw * 0.70)
        chest = frame_bgr[cy1:cy2, cx1:cx2]
        has_lanyard = has_nametag = 0.0
        if chest.size > 0:
            chsv = cv2.cvtColor(chest, cv2.COLOR_BGR2HSV)
            cH, cS, cV = chsv[:, :, 0], chsv[:, :, 1], chsv[:, :, 2]
            ct = cH.size or 1
            white = ((cV > 180) & (cS < 50)).sum() / ct
            has_nametag = 1.0 if white > 0.02 else 0.0
            orange = (((cH >= 5) & (cH <= 25) & (cS > 100) & (cV > 90)).sum() / ct)
            dark = ((cV < 50).sum() / ct)
            has_lanyard = 1.0 if (orange > 0.01 or dark > 0.10) else 0.0
        return 0.50 * correct_colour + 0.30 * has_lanyard + 0.20 * has_nametag
    except Exception:
        return None



class UniformComplianceDetector(Detector):
    detection_type = "uniform_compliance"
    needs_tracking = True

    def __init__(self):
        # (track_id, kind) -> last alert epoch.
        self._fired: dict[tuple[int, str], float] = {}
        # track_id -> (state, since_epoch) for sustained-duration alerts.
        self._state_since: dict[int, tuple[str, float]] = {}
        # store_id -> {"day": iso, "count": n} for the repeat-day rule.
        self._violations_today: dict[int, dict] = {}

    # ---- public ----------------------------------------------------

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        staff_zones = [z for z in ctx.zones
                       if STAFF_ZONE_TAGS & set(z.get("detection_types_json") or [])]
        # Without a staff/counter zone we have no way to tell staff from
        # customers, so we don't guess — operators must tag the counter.
        if not staff_zones:
            return []

        now = time.time()
        out: list[DetectionEvent] = []
        scored = 0
        ok_like = 0

        # Each person currently standing in a staff zone gets scored.
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON:
                continue
            if not any(bbox_in_zone(det["bbox_norm"], z["polygon_coords_json"])
                       for z in staff_zones):
                continue   # civilian / not at the counter — skip

            state = self._classify(ctx, det, cfg)
            if state == CIVILIAN:
                continue
            scored += 1
            if state in (OK, NO_LANYARD):
                ok_like += 1

            tid = self._match_track(ctx, det)
            evt = self._maybe_alert(ctx, det, tid, state, now)
            if evt is not None:
                out.append(evt)

        # Compliance metric — fraction of scored staff who are at least
        # in the right uniform (ok or no_lanyard count as "in uniform").
        if ctx.db is not None and scored:
            from app.analytics import recorder
            recorder.record(ctx.db, "uniform_compliance_pct",
                            ok_like / scored,
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            aggregator="avg")
        return out

    # ---- classification --------------------------------------------

    def _classify(self, ctx: DetectorContext, det: dict, cfg: dict) -> str:
        """Model-class first, then the colour rule-based fallback."""
        extra = cfg.get("extra") or {}
        thr = float(cfg.get("confidence_threshold", 0.5))
        # Mode 1: a custom model emitting the four classes.
        labels = {
            OK:        extra.get("class_ok",        "uniform_ok"),
            NO_LANYARD:extra.get("class_no_lanyard","no_lanyard"),
            VIOLATION: extra.get("class_violation", "uniform_violation"),
            CIVILIAN:  extra.get("class_civilian",  "civilian"),
        }
        best_state, best_conf = None, 0.0
        for state, label in labels.items():
            for d in ctx.raw_detections:
                if d["cls"] == label and d["conf"] >= thr:
                    # Same person? IOU against the person bbox.
                    if iou(d["bbox_norm"], det["bbox_norm"]) > 0.3 and d["conf"] > best_conf:
                        best_state, best_conf = state, d["conf"]
        if best_state is not None:
            return best_state

        # Mode 2: colour rule-based.
        score = self._colour_score(ctx, det, extra)
        if score is None:
            # No pixels to analyse — treat as civilian so we don't
            # false-alarm on a frame we couldn't read.
            return CIVILIAN
        if score >= 0.80:
            return OK
        if score >= 0.50:
            return NO_LANYARD
        return VIOLATION

    def _colour_score(self, ctx: DetectorContext, det: dict,
                      extra: dict) -> float | None:
        """0..1 compliance score from upper-body colour + lanyard +
        name-tag analysis. None if pixels can't be read."""
        try:
            import numpy as np
            import cv2
        except ImportError:
            return None
        frame = ctx.frame_bgr
        if frame is None or getattr(frame, "size", 0) == 0:
            return None
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = det["bbox_norm"]
        px1, py1 = int(max(0, x1) * w), int(max(0, y1) * h)
        px2, py2 = int(min(1, x2) * w), int(min(1, y2) * h)
        if px2 <= px1 or py2 <= py1:
            return None
        bh = py2 - py1

        # Upper body = top 40% of the person box (torso/shoulders).
        ub = frame[py1:py1 + int(bh * 0.40), px1:px2]
        if ub.size == 0:
            return None
        hsv = cv2.cvtColor(ub, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        total = H.size

        # Vivo BLACK top: low saturation, low value (dark fabric).
        black = ((S < 60) & (V < 70)).sum() / total
        # Vivo BURGUNDY/maroon: deep red. OpenCV H is 0..179, so red
        # wraps at both ends; burgundy is saturated + mid/low value.
        red_h = ((H <= 10) | (H >= 160))
        burgundy = (red_h & (S > 100) & (V > 40) & (V < 170)).sum() / total
        correct_colour = 1.0 if max(black, burgundy) >= 0.35 else \
                         (0.5 if max(black, burgundy) >= 0.20 else 0.0)

        # Chest band = middle 30% height, centre 40% width — where a
        # lanyard hangs and the name tag sits.
        cy1 = py1 + int(bh * 0.30)
        cy2 = py1 + int(bh * 0.60)
        cw = px2 - px1
        cx1 = px1 + int(cw * 0.30)
        cx2 = px1 + int(cw * 0.70)
        chest = frame[cy1:cy2, cx1:cx2]
        has_lanyard = has_nametag = 0.0
        if chest.size > 0:
            chsv = cv2.cvtColor(chest, cv2.COLOR_BGR2HSV)
            cH, cS, cV = chsv[:, :, 0], chsv[:, :, 1], chsv[:, :, 2]
            ct = cH.size
            # White name tag: bright + desaturated patch.
            white = ((cV > 180) & (cS < 50)).sum() / ct
            has_nametag = 1.0 if white > 0.02 else 0.0
            # Lanyard: orange strap (H 5..25, saturated) OR a dark
            # vertical strap (low V) crossing the chest.
            orange = (((cH >= 5) & (cH <= 25) & (cS > 100) & (cV > 90)).sum() / ct)
            dark   = ((cV < 50).sum() / ct)
            has_lanyard = 1.0 if (orange > 0.01 or dark > 0.10) else 0.0

        return 0.50 * correct_colour + 0.30 * has_lanyard + 0.20 * has_nametag

    # ---- alerting --------------------------------------------------

    def _match_track(self, ctx: DetectorContext, det: dict) -> int:
        for tr, _ in ctx.tracks:
            if tr.cls in COCO_PERSON and iou(tr.bbox_norm, det["bbox_norm"]) > 0.3:
                return tr.track_id
        # Stable-ish fallback id from the bbox when tracking misses.
        return hash(tuple(round(c, 2) for c in det["bbox_norm"])) & 0x7FFFFFFF

    def _maybe_alert(self, ctx: DetectorContext, det: dict, tid: int,
                     state: str, now: float) -> DetectionEvent | None:
        # Maintain the sustained-duration timer per track.
        prev = self._state_since.get(tid)
        if prev is None or prev[0] != state:
            self._state_since[tid] = (state, now)
            since = now
        else:
            since = prev[1]
        elapsed = now - since

        if state == VIOLATION and elapsed >= VIOLATION_SECONDS:
            if now - self._fired.get((tid, VIOLATION), 0) >= DEDUP_SECONDS:
                self._fired[(tid, VIOLATION)] = now
                self._bump_violation_count(ctx, now)
                repeated = self._violation_count_today(ctx) > REPEAT_THRESHOLD
                return DetectionEvent(
                    detection_type=self.detection_type, cls=VIOLATION,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "warning", "store_id": ctx.store_id,
                           "rule": "uniform_violation",
                           "shift": _shift_label(),
                           "repeated_today": repeated},
                )
        if state == NO_LANYARD and elapsed >= NO_LANYARD_SECONDS:
            if now - self._fired.get((tid, NO_LANYARD), 0) >= DEDUP_SECONDS:
                self._fired[(tid, NO_LANYARD)] = now
                return DetectionEvent(
                    detection_type=self.detection_type, cls=NO_LANYARD,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "info", "store_id": ctx.store_id,
                           "rule": "no_lanyard", "shift": _shift_label()},
                )
        return None

    def _bump_violation_count(self, ctx: DetectorContext, now: float) -> None:
        sid = ctx.store_id if ctx.store_id is not None else -1
        today = datetime.now(timezone.utc).date().isoformat()
        rec = self._violations_today.get(sid)
        if rec is None or rec.get("day") != today:
            rec = {"day": today, "count": 0}
        rec["count"] += 1
        self._violations_today[sid] = rec

    def _violation_count_today(self, ctx: DetectorContext) -> int:
        sid = ctx.store_id if ctx.store_id is not None else -1
        rec = self._violations_today.get(sid)
        today = datetime.now(timezone.utc).date().isoformat()
        return rec["count"] if rec and rec.get("day") == today else 0


def _shift_label() -> str:
    h = datetime.now(timezone.utc).hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"
