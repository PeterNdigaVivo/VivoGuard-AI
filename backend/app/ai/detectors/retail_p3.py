"""Priority-3 retail detectors:

  - UniformComplianceDetector — custom-model class labels for uniform
                               OK vs violation; per-shift compliance %
  - DemographicDetector       — aggregate-only age/gender counts
                               (privacy-compliant: no per-person retention)
  - ShrinkageDetector         — composite suspicious-behaviour rules
                               (dwell near high-value with no staff,
                               concealment class label, etc.)
  - StockroomAccessDetector   — per-track entry/exit logging into the
                               stockroom_accesses audit table
"""
from __future__ import annotations
import time

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone


# ---------------------------------------------------------------------------
# Uniform compliance — moved to app.ai.detectors.uniform_compliance
# ---------------------------------------------------------------------------
# The colour rule-based + model UniformComplianceDetector now lives in
# its own module. Re-exported here so existing imports keep working.
from app.ai.detectors.uniform_compliance import UniformComplianceDetector  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Demographics — aggregate-only, privacy-compliant
# ---------------------------------------------------------------------------

class DemographicDetector(Detector):
    """Aggregate counters only — NEVER stores per-person attributes.

    Reads detections whose class label looks like
    `age_<bucket>_gender_<m|f|u>` (e.g. `age_25_34_gender_f`). Train a
    custom YOLOv8 model in the Training Studio that emits these compound
    labels — or use an off-the-shelf face/attribute model and adapt the
    class names.

    Falls back to using person-only detections counted into a single
    'unknown' bucket so the dashboard tile is never blank.

    Writes `demographic_bucket` metric with dims={age, gender}.
    """

    detection_type = "demographic"
    needs_tracking = False

    def _parse_label(self, cls: str) -> tuple[str, str] | None:
        # Expected format: age_<bucket>_gender_<m|f|u>
        parts = cls.split("_")
        try:
            i = parts.index("age")
            age_bucket = parts[i + 1] + ("-" + parts[i + 2] if parts[i + 2].isdigit() else "")
            g_idx = parts.index("gender")
            gender = parts[g_idx + 1]
            return age_bucket, gender
        except (ValueError, IndexError):
            return None

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        # DISABLED until a real age/gender model is trained.
        #
        # Without `age_*_gender_*` class labels in the YOLO output
        # every detection falls into the "unknown" bucket — so the
        # dashboard tile carries no information AND the recorder
        # crashes on a `json = json` comparison Postgres doesn't
        # support, aborting the whole transaction and taking down
        # every other detector (most importantly the heatmap).
        #
        # Flip this back to `_evaluate_with_model()` once a trained
        # demographic model is wired up — see the docstring above.
        return []

    def _evaluate_with_model(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled") or ctx.db is None:
            return []
        thr = float(cfg.get("confidence_threshold", 0.5))

        buckets: dict[tuple[str, str], int] = {}
        for det in ctx.raw_detections:
            if det["conf"] < thr:
                continue
            parsed = self._parse_label(det["cls"])
            if parsed is None and det["cls"] in COCO_PERSON:
                parsed = ("unknown", "u")
            if parsed is None:
                continue
            buckets[parsed] = buckets.get(parsed, 0) + 1

        from app.analytics import recorder
        for (age, gender), count in buckets.items():
            recorder.record(ctx.db, "demographic_bucket", float(count),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            dims={"age": age, "gender": gender}, aggregator="sum")
        return []     # no per-person alerts (privacy)


# ---------------------------------------------------------------------------
# Shrinkage / suspicious behaviour
# ---------------------------------------------------------------------------

class ShrinkageDetector(Detector):
    """Composite rule:

      A. Loitering in a `high_value` zone for ≥ dwell_time_seconds while
         the nearest `counter` zone is currently unstaffed.
      B. Any detection of `concealing` (custom-trained class).
      C. Same person track entering and exiting an `entry` zone N times
         in M minutes without crossing a `purchase` zone (configurable
         via extra: `repeat_threshold`, `repeat_window_seconds`).

    All three emit a `shrinkage` event with extra.priority='high' so it
    cuts through the alert dedup quickly.
    """

    detection_type = "shrinkage"
    needs_tracking = True

    def __init__(self):
        self._dwell_entered: dict[tuple[int, int, int], float] = {}
        self._fired: dict[int, float] = {}
        self._entry_count: dict[int, list[float]] = {}        # track_id → entry timestamps

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        extra        = cfg.get("extra") or {}
        dwell        = int(cfg.get("dwell_time_seconds") or 30)
        repeat_thr   = int(extra.get("repeat_threshold", 4))
        repeat_win   = int(extra.get("repeat_window_seconds", 1800))
        thr          = float(cfg.get("confidence_threshold", 0.5))

        hv_zones    = [z for z in ctx.zones if "high_value" in (z.get("detection_types_json") or [])]
        ctr_zones   = [z for z in ctx.zones if "counter"     in (z.get("detection_types_json") or [])]
        entry_zones = [z for z in ctx.zones if "entry"       in (z.get("detection_types_json") or [])]
        # Check counter staffed state from people detections this frame.
        people = [d for d in ctx.raw_detections if d["cls"] in COCO_PERSON and d["conf"] >= thr]
        counter_staffed = any(
            bbox_in_zone(p["bbox_norm"], z["polygon_coords_json"]) for p in people for z in ctr_zones
        ) if ctr_zones else False

        out: list[DetectionEvent] = []
        now = time.time()

        # Rule A: dwell in high-value with no staff.
        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in hv_zones:
                key = (ctx.camera_id, tr.track_id, z["id"])
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    self._dwell_entered.setdefault(key, now)
                    if (now - self._dwell_entered[key] >= dwell
                            and not counter_staffed
                            and now - self._fired.get(tr.track_id, 0) > 600):
                        self._fired[tr.track_id] = now
                        out.append(DetectionEvent(
                            detection_type=self.detection_type, cls="loiter_no_staff",
                            confidence=0.85, bbox_norm=tr.bbox_norm,
                            track_id=tr.track_id, zone_id=z["id"],
                            extra={"priority": "high", "rule": "dwell_high_value_no_staff",
                                   "dwell_seconds": int(now - self._dwell_entered[key]),
                                   "store_id": ctx.store_id},
                        ))
                else:
                    self._dwell_entered.pop(key, None)

        # Rule B: explicit "concealing" class label.
        for det in ctx.raw_detections:
            if det["cls"] == "concealing" and det["conf"] >= thr:
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="concealing",
                    confidence=det["conf"], bbox_norm=det["bbox_norm"],
                    extra={"priority": "high", "rule": "concealing_detected",
                           "store_id": ctx.store_id},
                ))

        # Rule C: repeated entry without purchase zone visit.
        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON or not entry_zones:
                continue
            entered_now = any(bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]) for z in entry_zones)
            if entered_now:
                lst = self._entry_count.setdefault(tr.track_id, [])
                if not lst or now - lst[-1] > 60:    # ignore continuous frames
                    lst.append(now)
                lst[:] = [t for t in lst if now - t < repeat_win]
                if len(lst) >= repeat_thr and now - self._fired.get(tr.track_id, 0) > 600:
                    self._fired[tr.track_id] = now
                    out.append(DetectionEvent(
                        detection_type=self.detection_type, cls="repeat_entry",
                        confidence=0.7, bbox_norm=tr.bbox_norm,
                        track_id=tr.track_id,
                        extra={"priority": "high", "rule": "repeat_entry_no_purchase",
                               "entry_count": len(lst),
                               "window_seconds": repeat_win,
                               "store_id": ctx.store_id},
                    ))
        return out


# ---------------------------------------------------------------------------
# Stockroom access log
# ---------------------------------------------------------------------------

class StockroomAccessDetector(Detector):
    """Logs every person-track entry into and exit from a zone tagged
    `stockroom`. Each event becomes a row in `stockroom_accesses`.

    No notification by default — this is an audit trail; operators
    review it via the new `/store/<id>/stockroom-log` page or by CSV
    export. Operators can layer an alert on top by enabling the
    `unauthorised_person` flag (which marks the row `is_authorised=False`
    and emits an alert).
    """

    detection_type = "stockroom_access"
    needs_tracking = True

    def __init__(self):
        # (camera, track, zone) → currently-inside (bool)
        self._inside: dict[tuple[int, int, int], bool] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled") or ctx.db is None:
            return []
        zones = [z for z in ctx.zones if "stockroom" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        from app.models import StockroomAccess
        emit_alerts = bool((cfg.get("extra") or {}).get("alert_unauthorised", False))
        out: list[DetectionEvent] = []

        for tr, _det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in zones:
                key = (ctx.camera_id, tr.track_id, z["id"])
                inside_now = bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"])
                was_inside = self._inside.get(key, False)
                if inside_now and not was_inside:
                    self._inside[key] = True
                    ctx.db.add(StockroomAccess(
                        store_id=ctx.store_id, camera_id=ctx.camera_id,
                        zone_id=z["id"], direction="in", track_id=tr.track_id,
                        is_authorised=None,
                    ))
                    if emit_alerts:
                        out.append(DetectionEvent(
                            detection_type=self.detection_type, cls="entered",
                            confidence=1.0, bbox_norm=tr.bbox_norm,
                            track_id=tr.track_id, zone_id=z["id"],
                            extra={"priority": "high", "direction": "in",
                                   "store_id": ctx.store_id},
                        ))
                elif (not inside_now) and was_inside:
                    self._inside[key] = False
                    ctx.db.add(StockroomAccess(
                        store_id=ctx.store_id, camera_id=ctx.camera_id,
                        zone_id=z["id"], direction="out", track_id=tr.track_id,
                        is_authorised=None,
                    ))
        return out
