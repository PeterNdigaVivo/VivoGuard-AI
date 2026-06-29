"""Per-camera inference latency telemetry.

Modelled on SharpAI/DeepCamera's PerfTracker
(yolo-detection-2026/scripts/detect.py).

One PerfTracker per run_for_camera() loop. record() accumulates
per-stage durations; record_frame() ticks the frame counter and
flushes percentile stats every FLUSH_EVERY frames to:
  • Postgres InferencePerfLog — the TOTAL-stage p50/p95/p99/avg
    (historical record for GET /analytics/perf)
  • Redis vg:perf:{camera_id} (24h TTL) — the latest window's
    per-stage breakdown for the live System panel

emit_final() flushes the tail on shutdown.

Everything is best-effort — telemetry must NEVER slow or crash the
inference loop. Percentiles use statistics.quantiles (stdlib) so
there's no numpy in the hot path. Stage windows are cleared after
each flush so memory can't grow over a long-running camera loop.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

STAGES = ("frame_read", "inference", "postprocess", "emit", "total")
FLUSH_EVERY = 100
REDIS_KEY_FMT = "vg:perf:{camera_id}"
REDIS_TTL_S = 24 * 3600


def _summarise(samples: list[float]) -> dict:
    """avg/min/max/p50/p95/p99 for one stage's window. Empty → zeros.

    statistics.quantiles needs ≥2 points; for tiny windows we fall
    back to min/max so a flush on a near-empty tail never raises."""
    if not samples:
        return {"avg": 0.0, "min": 0.0, "max": 0.0,
                "p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}
    n = len(samples)
    avg = sum(samples) / n
    lo, hi = min(samples), max(samples)
    if n == 1:
        p50 = p95 = p99 = samples[0]
    else:
        s = sorted(samples)
        # quantiles(n=100) → 99 cut points; index i is the (i+1)th
        # percentile. p50=idx48, p95=idx93, p99=idx97 (0-based on the
        # 99-length list). Guard the index for short windows.
        cuts = statistics.quantiles(s, n=100, method="inclusive")
        def _pct(p: int) -> float:
            idx = min(len(cuts) - 1, max(0, p - 1))
            return cuts[idx]
        p50, p95, p99 = _pct(50), _pct(95), _pct(99)
    return {"avg": round(avg, 2), "min": round(lo, 2), "max": round(hi, 2),
            "p50": round(p50, 2), "p95": round(p95, 2),
            "p99": round(p99, 2), "n": n}


class PerfTracker:
    def __init__(self, camera_id: int, *, backend: str = "cpu",
                 fmt: str = "pytorch", redis_client=None) -> None:
        self.camera_id = int(camera_id)
        self.backend = backend
        self.fmt = fmt
        self._r = redis_client
        self.frame_count = 0
        # Per-stage rolling window (cleared on every flush).
        self._stages: dict[str, list[float]] = {s: [] for s in STAGES}

    # ------------------------------------------------------------------
    def record(self, stage: str, duration_ms: float) -> None:
        """Append a stage timing. Unknown stages are ignored rather
        than raising — telemetry must never break the caller."""
        bucket = self._stages.get(stage)
        if bucket is not None:
            bucket.append(float(duration_ms))

    def record_frame(self) -> None:
        """One frame processed. Flush every FLUSH_EVERY frames."""
        self.frame_count += 1
        if self.frame_count % FLUSH_EVERY == 0:
            self.emit_stats()

    # ------------------------------------------------------------------
    def emit_stats(self) -> None:
        """Compute per-stage summaries for the current window, write
        the TOTAL stage to Postgres + the full breakdown to Redis,
        then reset the windows. Best-effort: a DB/Redis failure logs
        and clears the window anyway so memory doesn't accumulate."""
        per_stage = {s: _summarise(self._stages[s]) for s in STAGES}
        total = per_stage["total"]
        window_n = total["n"]
        # Nothing to report (e.g. flush called with an empty window).
        if window_n == 0:
            self._reset()
            return

        # --- Redis: live per-stage breakdown for the System panel ----
        if self._r is not None:
            try:
                self._r.set(
                    REDIS_KEY_FMT.format(camera_id=self.camera_id),
                    json.dumps({
                        "camera_id":   self.camera_id,
                        "frame_count": self.frame_count,
                        "window_n":    window_n,
                        "backend":     self.backend,
                        "format":      self.fmt,
                        "ts":          time.time(),
                        "stages":      per_stage,    # full breakdown
                        # headline total numbers hoisted for cheap reads
                        "p50_ms":      total["p50"],
                        "p95_ms":      total["p95"],
                        "p99_ms":      total["p99"],
                        "avg_ms":      total["avg"],
                    }),
                    ex=REDIS_TTL_S,
                )
            except Exception as e:
                log.debug("perf: redis write failed cam=%s: %s",
                          self.camera_id, e)

        # --- Postgres: historical total-stage record -----------------
        try:
            from app.database import SessionLocal
            from app.models import InferencePerfLog
            with SessionLocal() as db:
                db.add(InferencePerfLog(
                    camera_id=self.camera_id,
                    timestamp=datetime.now(timezone.utc),
                    frame_count=window_n,
                    p50_ms=total["p50"], p95_ms=total["p95"],
                    p99_ms=total["p99"], avg_ms=total["avg"],
                    backend=self.backend, format=self.fmt,
                ))
                db.commit()
        except Exception as e:
            log.debug("perf: db write failed cam=%s: %s", self.camera_id, e)

        self._reset()

    def emit_final(self) -> None:
        """Flush the remaining (frame_count % FLUSH_EVERY) frames on
        shutdown. No-op when the last flush landed exactly on a
        boundary (windows already empty)."""
        if any(self._stages[s] for s in STAGES):
            self.emit_stats()

    # ------------------------------------------------------------------
    def _reset(self) -> None:
        for s in STAGES:
            self._stages[s].clear()
