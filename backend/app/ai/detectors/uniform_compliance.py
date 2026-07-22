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
import logging
import time
from datetime import datetime, timezone

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone, iou, zone_contains

log = logging.getLogger(__name__)


# Compliance state constants — also the alert `cls` strings.
# Spec P5: six-state classifier.
#   FULL_COMPLIANT    correct top + lanyard + nametag        → no alert
#   PARTIAL_COMPLIANT correct top + lanyard, no nametag      → ATTENTION 5min
#   COLOR_ONLY        correct top only                       → ATTENTION 5min (folded with partial)
#   NON_COMPLIANT     wrong colour / no uniform top          → URGENT 2min
#   CUSTOMER          not in staff zone                      → skip
#   UNCERTAIN         can't tell (low light / occluded)      → skip
FULL_COMPLIANT    = "full_compliant"
PARTIAL_COMPLIANT = "partial_compliant"
COLOR_ONLY        = "color_only"
NON_COMPLIANT     = "non_compliant"
CIVILIAN          = "customer"
UNCERTAIN         = "uncertain"
# Vivo all-black auto-harvest class (chain_training UNIFORM_LABELS).
# Treated identically to FULL_COMPLIANT for live-evaluation purposes:
# never fires a violation alert and marks the track as staff so the
# visitor count excludes it.
STAFF             = "staff"

# Legacy aliases kept so older callers / cached UI bundles keep working.
OK         = FULL_COMPLIANT       # uniform_ok
NO_LANYARD = PARTIAL_COMPLIANT    # no_lanyard
VIOLATION  = NON_COMPLIANT        # uniform_violation

_METRIC = {
    FULL_COMPLIANT:    1.0,
    PARTIAL_COMPLIANT: 0.7,
    COLOR_ONLY:        0.5,
    NON_COMPLIANT:     0.0,
}

# Sustained-duration thresholds (seconds) before an alert fires.
VIOLATION_SECONDS  = 2 * 60       # NON_COMPLIANT sustained
NO_LANYARD_SECONDS = 5 * 60       # PARTIAL/COLOR sustained
# Time in the staff/counter zone before we'll trust that someone really
# works there. NON_COMPLIANT alerts only fire after this dwell — a
# customer who briefly enters a staff zone in street clothes must not
# trigger a uniform-violation alert.
CONFIRMED_STAFF_SECONDS = 5 * 60
# Per (track, kind) dedup window.
DEDUP_SECONDS = 30 * 60
# Repeated-violations-today threshold.
REPEAT_THRESHOLD = 3

STAFF_ZONE_TAGS = {"counter", "staff", "staff_zone"}


def uniform_features(frame_bgr, bbox_norm) -> dict | None:
    """Vivo P5 colour analysis for one person bbox. Returns a dict of
    colour + accessory features, or None when pixels can't be read.

    UPPER BODY (top 0-50% of bbox):
      • MAROON  H in [160..179] or [0..8], S 90-255, V 40-220
      • BLACK   S ≤ 110, V ≤ 165   (wide envelope — uniform shadows)
    LOWER BODY (top 50-95% of bbox) — added for the Vivo all-black
                    uniform (top + bottom both dark):
      • BLACK   S ≤ 60,  V ≤ 80    (tighter than upper-body — trousers
                    are uniformly dark and we want to avoid
                    counting customer denim/jeans as match)
    LANYARD ZONE (top 15-65%, centre 30-70% of width):
      • orange: H 5..25, S ≥ 179, V ≥ 179
      • dark  : V < 128 (any H)
    NAMETAG ZONE (top 20-60%, centre 20-80% of width):
      • white rectangle: S < 77, V > 179

    Return dict keys:
      top_ok, top_share, top_black_share, top_maroon_share, top_is_black
      bottom_ok, bottom_share
      dual_black        — top_is_black AND bottom_ok (Vivo all-black gate)
      has_lanyard, has_nametag, confidence
    """
    try:
        import numpy as np  # noqa: F401
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
        bw = px2 - px1

        # Upper body (top 0..50% of bbox) for the top colour decision.
        ub = frame_bgr[py1:py1 + int(bh * 0.50), px1:px2]
        if ub.size == 0:
            return None
        hsv = cv2.cvtColor(ub, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        total = H.size or 1
        # Widened maroon/burgundy band — overhead store cameras see a
        # darker, less-saturated version of the uniform than the original
        # spec values caught (false "Unauthorised Person" alerts at Vivo
        # Junction, June 2026). H in OpenCV is 0..179 so the spec's
        # 0..15° and 340..360° map to H ≤ 7 and H ≥ 170; we widen further
        # to H ≤ 8 and H ≥ 160 to cover red-brown lighting drift. S and V
        # floors are dropped so dark, dim-light maroon still scores.
        maroon = (((H >= 160) | (H <= 8))
                  & (S >= 90) & (S <= 255)
                  & (V >= 40) & (V <= 220)).sum() / total
        # Black uniform — wider S/V envelope for shadows from overhead.
        black  = ((S <= 110) & (V <= 165)).sum() / total
        top_share = float(max(maroon, black))
        top_ok = top_share >= 0.20
        # Distinguish which colour band matched for the top — needed
        # for the Vivo "all-black uniform" gate that wants the TOP
        # specifically to be black (not just any uniform colour).
        top_black_share  = float(black)
        top_maroon_share = float(maroon)
        top_is_black     = bool(black >= 0.20 and black >= maroon)

        # Lower body (top 50..95% of bbox) — Vivo trousers are
        # uniformly dark; tighter HSV envelope than the upper body so
        # customers in dark jeans / denim don't trigger false
        # positives. Empty / out-of-frame slice → defaults to False.
        lb_y1 = py1 + int(bh * 0.50)
        lb_y2 = py1 + int(bh * 0.95)
        lb = frame_bgr[lb_y1:lb_y2, px1:px2]
        bottom_ok = False
        bottom_share = 0.0
        if lb.size > 0:
            lhsv = cv2.cvtColor(lb, cv2.COLOR_BGR2HSV)
            lS, lV = lhsv[:, :, 1], lhsv[:, :, 2]
            ltot = lS.size or 1
            lbblack = ((lS <= 60) & (lV <= 80)).sum() / ltot
            bottom_share = float(lbblack)
            bottom_ok = bool(lbblack >= 0.20)

        dual_black = bool(top_is_black and bottom_ok)

        # Lanyard zone — top 15..65%, centre 30..70% width.
        lz_y1 = py1 + int(bh * 0.15); lz_y2 = py1 + int(bh * 0.65)
        lz_x1 = px1 + int(bw * 0.30); lz_x2 = px1 + int(bw * 0.70)
        lanyard_zone = frame_bgr[lz_y1:lz_y2, lz_x1:lz_x2]
        has_lanyard = False
        if lanyard_zone.size > 0:
            lhsv = cv2.cvtColor(lanyard_zone, cv2.COLOR_BGR2HSV)
            lH, lS, lV = lhsv[:, :, 0], lhsv[:, :, 1], lhsv[:, :, 2]
            lt = lH.size or 1
            orange = (((lH >= 5) & (lH <= 25) & (lS >= 179) & (lV >= 179))
                      .sum() / lt)
            dark   = (lV < 128).sum() / lt
            has_lanyard = bool(orange >= 0.005 or dark >= 0.08)

        # Nametag zone — top 20..60%, centre 20..80% width. White card.
        nt_y1 = py1 + int(bh * 0.20); nt_y2 = py1 + int(bh * 0.60)
        nt_x1 = px1 + int(bw * 0.20); nt_x2 = px1 + int(bw * 0.80)
        nametag_zone = frame_bgr[nt_y1:nt_y2, nt_x1:nt_x2]
        has_nametag = False
        if nametag_zone.size > 0:
            nhsv = cv2.cvtColor(nametag_zone, cv2.COLOR_BGR2HSV)
            nS, nV = nhsv[:, :, 1], nhsv[:, :, 2]
            nt = nS.size or 1
            white = ((nS < 77) & (nV > 179)).sum() / nt
            has_nametag = bool(white >= 0.015)

        # Confidence — strengthened when the bottom half also reads
        # black. The dual-region signal is the strongest "this is a
        # Vivo uniform" tell we have without a trained classifier.
        confidence = min(1.0, top_share / 0.40)
        if dual_black:
            confidence = max(confidence, 0.85)
        return {
            "top_ok":           bool(top_ok),
            "top_share":        top_share,
            "top_black_share":  top_black_share,
            "top_maroon_share": top_maroon_share,
            "top_is_black":     top_is_black,
            "bottom_ok":        bottom_ok,
            "bottom_share":     bottom_share,
            "dual_black":       dual_black,
            "has_lanyard":      has_lanyard,
            "has_nametag":      has_nametag,
            "confidence":       confidence,
        }
    except Exception:
        return None


def uniform_colour_score(frame_bgr, bbox_norm) -> float | None:
    """Legacy 0..1 score retained for callers that haven't moved to
    uniform_features() yet (the StaffPresenceDetector colour fallback,
    the staff-track marker). Computed from the new features so the
    two paths agree."""
    feats = uniform_features(frame_bgr, bbox_norm)
    if feats is None:
        return None
    correct = 1.0 if feats["top_ok"] else 0.0
    return (0.50 * correct
            + 0.30 * (1.0 if feats["has_lanyard"] else 0.0)
            + 0.20 * (1.0 if feats["has_nametag"] else 0.0))



class UniformComplianceDetector(Detector):
    detection_type = "uniform_compliance"
    needs_tracking = True

    # P5: only act on a staff/uniform state once the track's detection
    # confidence has averaged >= this over >= this many frames — smooths
    # single-frame uniform misclassification (a key source of Counter Left
    # Unattended false alerts).
    STABLE_MIN_FRAMES = 10
    STABLE_MIN_AVG    = 0.65

    def __init__(self):
        from collections import deque as _deque
        # track_id -> rolling detection-confidence window (P5 stability gate).
        self._conf_hist: dict[int, "_deque"] = {}
        self._deque = _deque
        # (track_id, kind) -> last alert epoch.
        self._fired: dict[tuple[int, str], float] = {}
        # track_id -> (state, since_epoch) for sustained-duration alerts.
        self._state_since: dict[int, tuple[str, float]] = {}
        # store_id -> {"day": iso, "count": n} for the repeat-day rule.
        self._violations_today: dict[int, dict] = {}
        # Per-track dedup for the auto-crop harvester — capture at most
        # one frame per person per CROP_DEDUP_SECONDS so the training
        # queue doesn't fill with near-duplicates of the same person.
        self._last_cropped: dict[int, float] = {}

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
        # P4: PolygonZone (foot-point) containment when a frame is available.
        _wh = None
        if ctx.frame_bgr is not None:
            _h, _w = ctx.frame_bgr.shape[:2]
            _wh = (_w, _h)

        # Each person currently standing in a staff zone gets scored.
        for det in ctx.raw_detections:
            if det["cls"] not in COCO_PERSON:
                continue
            # Which (if any) staff/counter tag this detection sits in —
            # also feeds the shared time-in-zone registry.
            zone_tag: str | None = None
            for z in staff_zones:
                if zone_contains(det["bbox_norm"], z["polygon_coords_json"],
                                 zone_id=z["id"], frame_wh=_wh):
                    tags = set(z.get("detection_types_json") or [])
                    if "staff_zone" in tags:
                        zone_tag = "staff_zone"
                    else:
                        zone_tag = "counter"
                    break
            if zone_tag is None:
                continue   # civilian / not at the counter — skip

            tid = self._match_track(ctx, det)
            from app.ai.detectors import staff_identity
            staff_identity.observe(ctx.camera_id, tid, zone_tag, now)
            elapsed_in_zone = staff_identity.time_in_any_staff_zone(
                ctx.camera_id, tid, now)

            state = self._classify(ctx, det, cfg)
            # Skip non-scoreable people — customers (correct: outside
            # staff zones) and UNCERTAIN frames (low light / occluded
            # / too far). Both should NOT raise alerts.
            if state in (CIVILIAN, UNCERTAIN):
                continue
            scored += 1
            # "OK-like" = anyone in the right uniform colour, whether
            # they have the lanyard/tag or not. Used for the rolling
            # compliance metric. STAFF (Vivo all-black) is the strongest
            # ok-like signal — same accounting as FULL_COMPLIANT.
            if state in (FULL_COMPLIANT, PARTIAL_COMPLIANT, COLOR_ONLY, STAFF):
                ok_like += 1

            # P5 stability gate — require the track's detection confidence to
            # average >= STABLE_MIN_AVG over >= STABLE_MIN_FRAMES frames before
            # we act on this staff/uniform state (mark staff, fire violation).
            # A single strong-looking frame can't flip a customer to "staff".
            _buf = self._conf_hist.get(tid)
            if _buf is None:
                _buf = self._conf_hist[tid] = self._deque(maxlen=self.STABLE_MIN_FRAMES)
            _buf.append(float(det.get("conf", 0.0)))
            from app.config import settings as _s
            _min_avg = float(getattr(_s, "uniform_confidence_threshold",
                                     self.STABLE_MIN_AVG))
            if len(_buf) < self.STABLE_MIN_FRAMES or \
                    (sum(_buf) / len(_buf)) < _min_avg:
                continue

            # Staff exclusion — anyone scoring as uniformed staff (any
            # of the three uniform-present states plus the explicit
            # STAFF class) is excluded from the visitor / dwell counts
            # via the staff_tracks roster the analytics endpoints
            # LEFT-JOIN against. NON_COMPLIANT stays a potential
            # customer / unidentified person.
            if state in (FULL_COMPLIANT, PARTIAL_COMPLIANT, COLOR_ONLY, STAFF):
                staff_identity.mark_staff_track(
                    ctx, tid, source=("staff_class" if state == STAFF else "uniform"))

            # STAFF short-circuits the rest of the per-person logic —
            # no violation evaluation, no lanyard nag, no further alert
            # paths. The dual-region black uniform reading is the
            # strongest staff signal we have.
            if state == STAFF:
                continue

            # Confirmed-staff gate (P5 false-alert fix). A
            # NON_COMPLIANT reading on someone who just walked into
            # the staff zone is almost always a customer who strayed
            # behind the counter — NOT a uniform violation. Only fire
            # the warning once they've been in the zone long enough
            # to confirm they actually work there.
            if state == NON_COMPLIANT and elapsed_in_zone < CONFIRMED_STAFF_SECONDS:
                continue

            evt = self._maybe_alert(ctx, det, tid, state, now)
            if evt is not None:
                # Detection-time uniform-colour stamp (Part 6) — the
                # single source of truth read later by:
                #   • capture_alert_snapshot → paints orange box on the
                #     alert thumbnail via `extra.boxes`
                #   • absorb_confirmed / absorb_dismissed →
                #     label_from_uniform_color(extra.uniform_color)
                # for the preview-pair file.
                if state in (FULL_COMPLIANT, PARTIAL_COMPLIANT, COLOR_ONLY, STAFF):
                    feats = uniform_features(ctx.frame_bgr, det["bbox_norm"])
                    color = None
                    if feats:
                        if feats.get("top_is_black"):
                            color = "black"
                        elif feats.get("top_maroon_share", 0.0) >= 0.20:
                            color = "maroon"
                    if color:
                        merged = dict(evt.extra or {})
                        merged["uniform_color"] = color
                        merged["boxes"] = [{
                            "bbox":  det["bbox_norm"],
                            "color": "orange",
                            "label": f"{color.capitalize()} Uniform",
                        }]
                        evt.extra = merged
                out.append(evt)

            # Camera-zone training crop harvest — save a per-person crop
            # to the chain training queue, suggested-label tagged by
            # zone (staff_zone / counter → uniform; entry_exit / queue
            # → customer). 5-min dedup per track keeps the queue from
            # filling with near-duplicates of the same person.
            self._maybe_harvest_crop(ctx, det, tid, state, now)

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
        """Seven-state classification: FULL_COMPLIANT / PARTIAL_COMPLIANT
        / COLOR_ONLY / NON_COMPLIANT / CUSTOMER / UNCERTAIN / STAFF.

        Model-class first, then the colour rule-based fallback."""
        extra = cfg.get("extra") or {}
        thr = float(cfg.get("confidence_threshold", 0.5))
        # Mode 1: custom model emitting the seven canonical classes
        # OR the legacy four. List of (state, class-name) — was a dict,
        # but dict keys silently overwrote: the three legacy aliases
        # at the bottom (uniform_ok / no_lanyard / uniform_violation)
        # were stomping the canonical names above, so any chain model
        # emitting "full_compliant" / "partial_compliant" / "non_compliant"
        # would never match. The list form preserves every entry.
        labels = [
            (FULL_COMPLIANT,    extra.get("class_full_compliant",    "full_compliant")),
            (PARTIAL_COMPLIANT, extra.get("class_partial_compliant", "partial_compliant")),
            (COLOR_ONLY,        extra.get("class_color_only",        "color_only")),
            (NON_COMPLIANT,     extra.get("class_non_compliant",     "non_compliant")),
            (CIVILIAN,          extra.get("class_customer",          "customer")),
            (UNCERTAIN,         extra.get("class_uncertain",         "uncertain")),
            (STAFF,             extra.get("class_staff",             "staff")),
            # Legacy 4-class names kept so older deployed models keep
            # mapping correctly. Each maps to the closest canonical
            # state.
            (FULL_COMPLIANT,    extra.get("class_ok",        "uniform_ok")),
            (PARTIAL_COMPLIANT, extra.get("class_no_lanyard","no_lanyard")),
            (NON_COMPLIANT,     extra.get("class_violation", "uniform_violation")),
        ]
        best_state, best_conf = None, 0.0
        for state, label in labels:
            for d in ctx.raw_detections:
                if d["cls"] == label and d["conf"] >= thr:
                    if iou(d["bbox_norm"], det["bbox_norm"]) > 0.3 and d["conf"] > best_conf:
                        best_state, best_conf = state, d["conf"]
        if best_state is not None:
            return best_state

        # Mode 2: colour rule-based, six-state decision tree.
        feats = uniform_features(ctx.frame_bgr, det["bbox_norm"])
        if feats is None or feats["confidence"] < 0.25:
            # Pixels missing or top didn't match anything cleanly — let
            # the caller skip (no alert) rather than false-alarm. The
            # 0.25 floor (top_share ≥ ~0.10) lets borderline overhead
            # shots fall into UNCERTAIN instead of NON_COMPLIANT.
            return UNCERTAIN
        if not feats["top_ok"]:
            # Borderline colour read — a non-trivial fraction matched
            # the uniform band but didn't cross the top_ok floor. Treat
            # as UNCERTAIN so we don't fire a violation on someone the
            # camera can't read reliably (typical for overhead angles
            # + low light). Only call NON_COMPLIANT when the share is
            # decisively low.
            if feats.get("top_share", 0.0) >= 0.10:
                return UNCERTAIN
            return NON_COMPLIANT
        if feats["has_lanyard"] and feats["has_nametag"]:
            return FULL_COMPLIANT
        if feats["has_lanyard"]:
            return PARTIAL_COMPLIANT
        return COLOR_ONLY

    # ---- alerting --------------------------------------------------

    def _match_track(self, ctx: DetectorContext, det: dict) -> int:
        for tr, _ in ctx.tracks:
            if tr.cls in COCO_PERSON and iou(tr.bbox_norm, det["bbox_norm"]) > 0.3:
                return tr.track_id
        # Stable-ish fallback id from the bbox when tracking misses.
        return hash(tuple(round(c, 2) for c in det["bbox_norm"])) & 0x7FFFFFFF

    # ---- training-crop harvest -------------------------------------

    CROP_DEDUP_SECONDS = 5 * 60          # 1 crop / person / 5 min
    PENDING_CAP        = 500             # pause harvest above this
    # Suggested label per source zone tag — spec P1.
    _ZONE_TO_LABEL = {
        "staff_zone": "uniform_ok",
        "counter":    "uniform_ok",
        "queue":      "civilian",
        "entry_exit": "civilian",
    }

    def _maybe_harvest_crop(self, ctx: DetectorContext, det: dict,
                            tid: int, state: str, now: float) -> None:
        """Save a cropped person frame into training_samples (source=
        'camera_crop', approved=null pending review) when this is a
        fresh sighting AND the pending-review queue still has room.
        Best-effort — never raises."""
        if ctx.db is None or ctx.frame_bgr is None:
            return
        if state == UNCERTAIN:
            return   # blurry / unsure — not useful training signal
        if now - self._last_cropped.get(tid, 0) < self.CROP_DEDUP_SECONDS:
            return
        # Decide which zone (if any) contains this detection — that
        # tells us the suggested label.
        zone_tag, crop_kind = self._zone_for_crop(ctx, det["bbox_norm"])
        if zone_tag is None:
            return   # only harvest from the four labelled zones
        try:
            from app.models import TrainingSample
            pending = (ctx.db.query(TrainingSample.id)
                          .filter(TrainingSample.detector_type == "uniform",
                                  TrainingSample.source == "camera_crop",
                                  TrainingSample.approved.is_(None))
                          .count())
        except Exception:
            pending = 0
        if pending >= self.PENDING_CAP:
            return   # back-pressure until the review queue catches up

        # Crop person bbox; for `counter` zones we keep the spec's
        # upper-body 60% crop (better signal for the uniform check).
        path = self._write_crop(ctx, det["bbox_norm"], crop_kind)
        if not path:
            return

        try:
            from datetime import datetime, timezone
            sample = TrainingSample(
                detector_type="uniform",
                label=self._ZONE_TO_LABEL.get(zone_tag, "civilian"),
                camera_id=ctx.camera_id, store_id=ctx.store_id,
                frame_path=path,
                captured_at=datetime.now(timezone.utc),
                source="camera_crop",
                shared=True,
                approved=None,           # pending operator review
            )
            ctx.db.add(sample)
            ctx.db.flush()
            # Preview crop (Part 6) — orange overlay for operator
            # review. Best-effort: must not block the clean save.
            # Re-derive colour family from uniform_features so the
            # label is precise; absent for civilians (label="").
            try:
                from app.training.image_preview import write_preview, label_from_uniform_color
                feats = uniform_features(ctx.frame_bgr, det["bbox_norm"])
                color = None
                if feats:
                    if feats.get("top_is_black"):
                        color = "black"
                    elif feats.get("top_maroon_share", 0.0) >= 0.20:
                        color = "maroon"
                preview = write_preview(
                    path,
                    label=label_from_uniform_color(color),
                    bbox_norm=None,
                )
                if preview:
                    sample.preview_path = preview
                    ctx.db.flush()
            except Exception:
                log.exception("uniform-compliance harvest: preview write "
                              "failed cam=%s tid=%s", ctx.camera_id, tid)
            self._last_cropped[tid] = now
        except Exception:
            try: ctx.db.rollback()
            except Exception: pass

    @staticmethod
    def _zone_for_crop(ctx: DetectorContext, bbox_norm) -> tuple[str | None, str]:
        """Find the zone tag (staff_zone / counter / queue / entry_exit)
        the detection sits in. Returns (zone_tag, crop_kind) where
        crop_kind is 'upper' for counter (top 60% of bbox) and 'full'
        otherwise."""
        for z in ctx.zones:
            tags = set(z.get("detection_types_json") or [])
            if not (tags & {"staff_zone", "counter", "queue", "entry_exit"}):
                continue
            if bbox_in_zone(bbox_norm, z.get("polygon_coords_json")):
                if "staff_zone" in tags: return "staff_zone", "full"
                if "counter"    in tags: return "counter",    "upper"
                if "queue"      in tags: return "queue",      "full"
                if "entry_exit" in tags: return "entry_exit", "full"
        return None, "full"

    def _write_crop(self, ctx: DetectorContext, bbox_norm, crop_kind: str) -> str | None:
        """Write the cropped JPEG to /data/training/uniform/_camera_crops/
        and return the on-disk path."""
        try:
            import cv2
            from pathlib import Path
            from datetime import datetime, timezone
            from app.config import settings
            frame = ctx.frame_bgr
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox_norm
            px1, py1 = int(max(0, x1) * w), int(max(0, y1) * h)
            px2, py2 = int(min(1, x2) * w), int(min(1, y2) * h)
            if px2 <= px1 or py2 <= py1:
                return None
            if crop_kind == "upper":
                py2 = py1 + int((py2 - py1) * 0.60)
            # P6: sv.crop_image for a clean, consistent person crop into the
            # training pipeline; fall back to a plain array slice if
            # supervision isn't available.
            try:
                import supervision as sv
                import numpy as _np
                crop = sv.crop_image(frame, _np.array([px1, py1, px2, py2]))
            except Exception:
                crop = frame[py1:py2, px1:px2]
            if crop is None or crop.size == 0:
                return None
            root = (Path(settings.datasets_dir).parent
                    / "training" / "uniform" / "_camera_crops")
            root.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            path = root / f"cam{ctx.camera_id}_{ts}.jpg"
            cv2.imwrite(str(path), crop)
            return str(path)
        except Exception:
            return None

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

        if state == NON_COMPLIANT and elapsed >= VIOLATION_SECONDS:
            if now - self._fired.get((tid, NON_COMPLIANT), 0) >= DEDUP_SECONDS:
                self._fired[(tid, NON_COMPLIANT)] = now
                self._bump_violation_count(ctx, now)
                repeated = self._violation_count_today(ctx) > REPEAT_THRESHOLD
                return DetectionEvent(
                    detection_type=self.detection_type, cls=NON_COMPLIANT,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "warning", "store_id": ctx.store_id,
                           "rule": "uniform_violation",
                           "shift": _shift_label(),
                           "repeated_today": repeated},
                )
        # Partial compliance and "right colour but no lanyard" both get
        # the gentle 5-minute INFO nudge — same operator action.
        if state in (PARTIAL_COMPLIANT, COLOR_ONLY) and elapsed >= NO_LANYARD_SECONDS:
            kind = state
            if now - self._fired.get((tid, kind), 0) >= DEDUP_SECONDS:
                self._fired[(tid, kind)] = now
                rule = "no_lanyard" if state == PARTIAL_COMPLIANT else "color_only"
                return DetectionEvent(
                    detection_type=self.detection_type, cls=kind,
                    confidence=1.0, bbox_norm=det["bbox_norm"], track_id=tid,
                    extra={"priority": "info", "store_id": ctx.store_id,
                           "rule": rule, "shift": _shift_label()},
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
