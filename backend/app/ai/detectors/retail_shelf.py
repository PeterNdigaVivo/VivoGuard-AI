"""Shelf-emptied detector — image-difference heuristic.

Operator tags a zone 'shelf_change'. The detector keeps a rolling
baseline of the zone's grayscale histogram. When the histogram diverges
significantly from baseline (an item was removed, exposing the shelf
back, or a whole rack went empty) it fires a 'shelf_change' event.

Tunables in detection_configs.extra:
  baseline_seconds       (default 600) — how long to average before locking baseline
  divergence_threshold   (default 0.35) — chi-square distance threshold
  reset_after_change     (default true) — re-baseline after a change fires

False positives: lighting changes, mannequin re-dressing. Operators can
tune divergence_threshold higher to reduce noise.
"""
from __future__ import annotations
import time

from app.ai.detectors.base import (
    Detector, DetectorContext, DetectionEvent,
)


class ShelfChangeDetector(Detector):
    detection_type = "shelf_change"
    needs_tracking = False

    def __init__(self):
        # zone_id → {"hist": list[float], "count": int, "locked": bool, "fired_at": float}
        self._baselines: dict[int, dict] = {}

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled") or ctx.frame_bgr is None:
            return []
        zones = [z for z in ctx.zones if "shelf_change" in (z.get("detection_types_json") or [])]
        if not zones:
            return []

        extra = cfg.get("extra") or {}
        baseline_seconds = float(extra.get("baseline_seconds", 600))
        thr              = float(extra.get("divergence_threshold", 0.35))
        reset_after      = bool(extra.get("reset_after_change", True))

        try:
            import cv2
            import numpy as np
        except ImportError:
            return []

        h, w = ctx.frame_bgr.shape[:2]
        out: list[DetectionEvent] = []
        now = time.time()

        for z in zones:
            poly = z.get("polygon_coords_json")
            if not poly or len(poly) < 3:
                continue
            pts = np.array([[int(p[0] * w), int(p[1] * h)] for p in poly], dtype=np.int32)
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(mask, [pts], 255)
            gray = cv2.cvtColor(ctx.frame_bgr, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], mask, [16], [0, 256]).flatten()
            s = float(hist.sum())
            if s <= 0:
                continue
            hist = hist / s     # normalise

            state = self._baselines.setdefault(z["id"], {
                "hist": None, "count": 0, "since": now,
                "locked": False, "fired_at": 0.0,
            })

            if not state["locked"]:
                # Build the baseline as a running mean for `baseline_seconds`.
                if state["hist"] is None:
                    state["hist"] = hist.tolist()
                    state["count"] = 1
                else:
                    n = state["count"] + 1
                    state["hist"] = [(a * (n - 1) + b) / n for a, b in zip(state["hist"], hist)]
                    state["count"] = n
                if now - state["since"] >= baseline_seconds:
                    state["locked"] = True
                continue

            # Compare current hist vs locked baseline with chi-square.
            base = state["hist"]
            chi = sum(((bi - hi) ** 2) / (bi + hi + 1e-9)
                      for bi, hi in zip(base, hist.tolist())) * 0.5
            if chi >= thr and now - state["fired_at"] > 60:
                state["fired_at"] = now
                out.append(DetectionEvent(
                    detection_type=self.detection_type, cls="shelf_change",
                    confidence=min(1.0, chi / thr),
                    bbox_norm=[float(pts[:, 0].min()) / w, float(pts[:, 1].min()) / h,
                               float(pts[:, 0].max()) / w, float(pts[:, 1].max()) / h],
                    zone_id=z["id"],
                    extra={"chi_square": round(chi, 3), "threshold": thr,
                           "store_id": ctx.store_id},
                ))
                if reset_after:
                    state["hist"] = None
                    state["count"] = 0
                    state["since"] = now
                    state["locked"] = False
        return out
