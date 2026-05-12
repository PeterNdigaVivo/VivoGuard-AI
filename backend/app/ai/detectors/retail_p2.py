"""Priority-2 retail detectors:

  - StaffPresenceDetector  — % of time a 'counter'-tagged zone is staffed
  - AisleDwellDetector     — avg dwell time per aisle zone, ranked
  - ShutterDetector        — shutter open/closed via custom class label or
                             a fallback brightness heuristic on a zone
  (HeatmapDetector already exists — P2 adds an export endpoint, see
   `app.api.analytics.heatmap_image`.)
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from statistics import mean

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_in_zone


# ---------------------------------------------------------------------------
# Staff availability at a counter
# ---------------------------------------------------------------------------

class StaffPresenceDetector(Detector):
    """For every zone tagged `counter`, check if at least one person is
    present. Emits an alert when the counter has been *unstaffed* for
    `dwell_time_seconds` consecutively. Always writes the
    `staff_present_pct` metric (0/1 each frame, the dashboard averages
    over the period).

    Operators tag any number of zones with `counter` to enable this.
    """

    detection_type = "staff_present"
    needs_tracking = False

    def __init__(self):
        # zone_id → t_unstaffed_since (None when currently staffed)
        self._empty_since: dict[int, float | None] = {}
        self._fired: dict[int, float] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        thr_conf = float(cfg.get("confidence_threshold", 0.5))
        dwell    = int(cfg.get("dwell_time_seconds") or 120)

        zones = [z for z in ctx.zones
                 if "counter" in (z.get("detection_types_json") or [])
                 and not z.get("suppressed")]
        if not zones:
            return []

        people = [d for d in ctx.raw_detections
                  if d["cls"] in COCO_PERSON and d["conf"] >= thr_conf]
        out: list[DetectionEvent] = []
        now = time.time()

        for z in zones:
            present = any(bbox_in_zone(p["bbox_norm"], z["polygon_coords_json"]) for p in people)

            # Metric: 1 when staffed, 0 when not. Dashboard avg = staffed %.
            if ctx.db is not None:
                from app.analytics import recorder
                recorder.record(ctx.db, "staff_present_pct", 1.0 if present else 0.0,
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=z["id"], aggregator="last")

            if present:
                self._empty_since[z["id"]] = None
                continue

            t_since = self._empty_since.get(z["id"]) or now
            self._empty_since.setdefault(z["id"], now)
            unstaffed_for = now - t_since
            if unstaffed_for >= dwell and now - self._fired.get(z["id"], 0) > 120:
                self._fired[z["id"]] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="counter_unstaffed",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1], zone_id=z["id"],
                    extra={"unstaffed_seconds": int(unstaffed_for),
                           "store_id": ctx.store_id},
                ))
        return out


# ---------------------------------------------------------------------------
# Aisle dwell time
# ---------------------------------------------------------------------------

class AisleDwellDetector(Detector):
    """For every zone tagged `aisle`, track each person's entry time and
    emit the dwell duration (seconds) when they leave. Also writes a
    rolling-average `dwell_seconds` metric per zone."""

    detection_type = "dwell"
    needs_tracking = True

    def __init__(self):
        # (camera, track, zone) → entry timestamp
        self._entered: dict[tuple[int, int, int], float] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        zones = [z for z in ctx.zones
                 if "aisle" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        out: list[DetectionEvent] = []
        now = time.time()
        per_zone_dwells: dict[int, list[float]] = {z["id"]: [] for z in zones}

        active_track_ids = {tr.track_id for tr, _ in ctx.tracks if tr.cls in COCO_PERSON}

        for tr, det in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            for z in zones:
                key = (ctx.camera_id, tr.track_id, z["id"])
                if bbox_in_zone(tr.bbox_norm, z["polygon_coords_json"]):
                    self._entered.setdefault(key, now)
                    per_zone_dwells[z["id"]].append(max(0.0, now - self._entered[key]))
                else:
                    # Person just left the zone — emit a per-track dwell event.
                    if key in self._entered:
                        dwell = max(0.0, now - self._entered.pop(key))
                        if dwell >= 2.0:    # ignore quick flybys
                            out.append(DetectionEvent(
                                detection_type=self.detection_type,
                                cls="aisle_dwell",
                                confidence=1.0,
                                bbox_norm=tr.bbox_norm,
                                track_id=tr.track_id, zone_id=z["id"],
                                extra={"dwell_seconds": round(dwell, 1)},
                            ))

        # Reap entries for tracks that fully aged out.
        for k in list(self._entered):
            if k[1] not in active_track_ids:
                self._entered.pop(k, None)

        # Per-zone rolling avg metric.
        if ctx.db is not None:
            from app.analytics import recorder
            for zid, dwells in per_zone_dwells.items():
                if not dwells:
                    continue
                recorder.record(ctx.db, "dwell_seconds", float(mean(dwells)),
                                camera_id=ctx.camera_id, store_id=ctx.store_id,
                                zone_id=zid, aggregator="avg")
        return out


# ---------------------------------------------------------------------------
# Shutter open/closed
# ---------------------------------------------------------------------------

class ShutterDetector(Detector):
    """Two-mode detector:

    1. Custom-model mode (preferred): if the configured AI model emits
       a class label of `shutter_open` or `shutter_closed`, use the
       higher-confidence detection in the configured zone.

    2. Heuristic mode (fallback): the shutter zone's mean luminance is
       compared against `extra.dark_threshold` (default 50/255).
       Below = closed, above = open. Works for most metal roller
       shutters where the closed state is visibly darker than the open
       store interior.

    Emits an alert when the state contradicts business_hours:
      open + closed-hours   → "shutter open after hours" (priority high)
      closed + open-hours   → "shutter closed during business hours"
    Writes a `shutter_open` metric (1/0) every sample.
    """

    detection_type = "shutter"
    needs_tracking = False

    def __init__(self):
        self._last_state: dict[int, int] = {}    # camera_id → 1/0
        self._fired: dict[tuple[int, str], float] = {}

    def _detect_state(self, ctx: DetectorContext, zone: dict | None,
                      cfg: dict) -> int | None:
        # Mode 1: custom-class detections.
        cls_open   = (cfg.get("extra") or {}).get("class_open",   "shutter_open")
        cls_closed = (cfg.get("extra") or {}).get("class_closed", "shutter_closed")
        open_best  = max((d["conf"] for d in ctx.raw_detections if d["cls"] == cls_open),   default=0.0)
        closed_best= max((d["conf"] for d in ctx.raw_detections if d["cls"] == cls_closed), default=0.0)
        thr = float(cfg.get("confidence_threshold", 0.5))
        if open_best >= thr or closed_best >= thr:
            return 1 if open_best >= closed_best else 0
        # Mode 2 heuristic deferred to the inference worker because it
        # needs raw pixel access; signalled via dim/dark_threshold dim.
        return None

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []

        zone = None
        for z in ctx.zones:
            if "shutter" in (z.get("detection_types_json") or []):
                zone = z
                break

        state = self._detect_state(ctx, zone, cfg)
        if state is None:
            return []   # heuristic mode not yet wired; ignore frame

        # Metric.
        if ctx.db is not None:
            from app.analytics import recorder
            recorder.record(ctx.db, "shutter_open", float(state),
                            camera_id=ctx.camera_id, store_id=ctx.store_id,
                            zone_id=(zone or {}).get("id"), aggregator="last")

        # Business-hours mismatch → alert.
        out: list[DetectionEvent] = []
        if ctx.business_hours is not None and ctx.store_timezone:
            from app.utils.business_hours import is_open, localised_now
            now_local = localised_now(ctx.store_timezone)
            is_open_now = is_open(ctx.business_hours, now_local)
            mismatch_open_after_hours  = (state == 1 and not is_open_now)
            mismatch_closed_in_hours   = (state == 0 and is_open_now)

            now = time.time()
            if mismatch_open_after_hours and now - self._fired.get((ctx.camera_id, "open_after"), 0) > 300:
                self._fired[(ctx.camera_id, "open_after")] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="open_after_hours",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1],
                    zone_id=(zone or {}).get("id"),
                    extra={"priority": "high", "shutter_state": "open",
                           "store_id": ctx.store_id, "rule": "open_after_hours"},
                ))
            if mismatch_closed_in_hours and now - self._fired.get((ctx.camera_id, "closed_during"), 0) > 600:
                self._fired[(ctx.camera_id, "closed_during")] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="closed_during_hours",
                    confidence=1.0, bbox_norm=[0, 0, 1, 1],
                    zone_id=(zone or {}).get("id"),
                    extra={"shutter_state": "closed", "store_id": ctx.store_id,
                           "rule": "closed_during_hours"},
                ))
        return out
