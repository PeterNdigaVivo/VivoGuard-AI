"""GPU multi-camera batching coordinator (telemetry-only dark launch).

This service deliberately does not persist detections, update trackers or emit
alerts. It reads the same freshest Redis frames as the authoritative inference
loop, groups cameras by model weights, runs bounded batch inference, and writes
capacity/fairness evidence to ``vg:inference:batch-shadow-health``.

Promotion to an authoritative coordinator requires a separate reviewed change;
setting ``INFERENCE_BATCH_SHADOW_MODE=false`` currently fails closed.
"""
from __future__ import annotations

import io
import json
import logging
import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace

import numpy as np
import redis
from PIL import Image

from app.ai.env_config import HardwareEnv
from app.ai.inference_worker import _load_camera_state
from app.ai.yolov8_runner import infer_batch
from app.config import settings
from app.database import SessionLocal
from app.stream.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)

HEALTH_KEY = "vg:inference:batch-shadow-health"

_CRITICAL_TYPES = {
    "weapon", "weapon_brandished", "fire", "smoke", "fall",
    "intrusion", "trespass",
}
_HIGH_RISK_TYPES = {
    "tailgating", "loitering", "staff_present", "uniform_compliance",
    "checkout_dwell", "abandoned_object",
}


@dataclass(frozen=True)
class BatchCandidate:
    camera_id: int
    frame_ts: float
    frame: np.ndarray | None
    weights: str
    priority: int = 1


def risk_priority(config: dict) -> int:
    """Return bounded scheduling priority from enabled detector ownership."""
    enabled = {
        detection_type
        for detection_type, value in config.items()
        if bool((value or {}).get("enabled"))
    }
    if enabled & _CRITICAL_TYPES:
        return 3
    if enabled & _HIGH_RISK_TYPES:
        return 2
    return 1


class WeightedFairBatchScheduler:
    """Weighted fair scheduler with starvation-free virtual finish times.

    Model weights cannot share an Ultralytics batch, so the scheduler selects
    one model group per call. Priority only moves a camera's effective deadline
    through a smaller virtual-service increment. Unserved cameras start at
    zero, so every active feed is selected before a higher-priority feed can
    consume a second turn.
    """

    def __init__(self):
        self.last_served: dict[int, float] = {}
        self.first_seen: dict[int, float] = {}
        self.virtual_finish: dict[int, float] = {}

    def _rank(self, candidate: BatchCandidate, now: float) -> tuple:
        self.first_seen.setdefault(candidate.camera_id, now)
        return (
            self.virtual_finish.get(candidate.camera_id, 0.0),
            -candidate.priority,
            candidate.camera_id,
        )

    def select(self, candidates: list[BatchCandidate], *, batch_size: int,
               now: float) -> list[BatchCandidate]:
        if not candidates or batch_size < 1:
            return []
        groups: dict[str, list[BatchCandidate]] = defaultdict(list)
        for candidate in candidates:
            groups[candidate.weights].append(candidate)
        ordered_groups = []
        for weights, group in groups.items():
            ordered = sorted(
                group,
                key=lambda candidate: self._rank(candidate, now),
            )
            ordered_groups.append((self._rank(ordered[0], now), weights, ordered))
        _, _, selected_group = min(ordered_groups, key=lambda item: (item[0], item[1]))
        selected = selected_group[:batch_size]
        for candidate in selected:
            self.last_served[candidate.camera_id] = now
            self.virtual_finish[candidate.camera_id] = (
                self.virtual_finish.get(candidate.camera_id, 0.0)
                + 1.0 / max(1, candidate.priority)
            )
        return selected

    def max_wait_seconds(self, active_camera_ids: set[int], *, now: float) -> float:
        waits = [
            now - self.last_served.get(camera_id, self.first_seen.get(camera_id, now))
            for camera_id in active_camera_ids
        ]
        return max(waits, default=0.0)

    def prune(self, active_camera_ids: set[int]) -> None:
        for mapping in (self.last_served, self.first_seen, self.virtual_finish):
            for camera_id in set(mapping) - active_camera_ids:
                mapping.pop(camera_id, None)


class BatchShadowCoordinator:
    def __init__(self):
        self.buffer = FrameBuffer()
        self.redis = redis.from_url(settings.redis_url)
        self.scheduler = WeightedFairBatchScheduler()
        self.specs: dict[int, tuple[str, int]] = {}
        self.last_processed_ts: dict[int, float] = {}
        self.last_refresh = 0.0
        self.started = time.time()
        self.batches = 0
        self.frames = 0
        self.errors = 0
        self.detections = 0
        self.latencies_ms: deque[float] = deque(maxlen=500)
        self.per_frame_latencies_ms: deque[float] = deque(maxlen=500)
        self.last_health_write = 0.0

    def refresh_specs(self, *, now: float) -> None:
        if now - self.last_refresh < settings.inference_batch_refresh_seconds:
            return
        from app.models import Camera

        specs: dict[int, tuple[str, int]] = {}
        with SessionLocal() as db:
            ids = [
                int(row[0])
                for row in db.query(Camera.id).filter(
                    Camera.ai_enabled.is_(True), Camera.is_deleted.is_(False),
                ).all()
            ]
            for camera_id in ids:
                cam, _zones, config, weights = _load_camera_state(db, camera_id)
                if cam is not None:
                    specs[camera_id] = (weights, risk_priority(config))
        self.specs = specs
        active = set(specs)
        self.scheduler.prune(active)
        for camera_id in set(self.last_processed_ts) - active:
            self.last_processed_ts.pop(camera_id, None)
        self.last_refresh = now

    def candidates(self) -> list[BatchCandidate]:
        """Collect lightweight frame metadata; pixels are decoded after select."""
        candidates = []
        for camera_id, (weights, priority) in self.specs.items():
            health = self.buffer.health(camera_id) or {}
            frame_ts = float(health.get("last_frame_at") or 0.0)
            if frame_ts <= self.last_processed_ts.get(camera_id, 0.0):
                continue
            candidates.append(BatchCandidate(
                camera_id=camera_id,
                frame_ts=frame_ts,
                frame=None,
                weights=weights,
                priority=priority,
            ))
        return candidates

    def decode_selected(
        self, selected: list[BatchCandidate],
    ) -> list[BatchCandidate]:
        """Decode at most one bounded batch, never the entire camera fleet."""
        decoded = []
        for candidate in selected:
            jpeg = self.buffer.latest_jpeg(
                candidate.camera_id, prefer_overlay=False,
            )
            if not jpeg:
                continue
            try:
                image = Image.open(io.BytesIO(jpeg)).convert("RGB")
                frame = np.array(image)[:, :, ::-1]
            except Exception as exc:
                self.errors += 1
                log.warning(
                    "batch shadow: bad jpeg camera=%s: %s",
                    candidate.camera_id, exc,
                )
                # Do not retry a permanently malformed frame forever.
                self.last_processed_ts[candidate.camera_id] = candidate.frame_ts
                continue
            decoded.append(replace(candidate, frame=frame))
        return decoded

    def process_once(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        self.refresh_specs(now=now)
        candidates = self.candidates()
        selected = self.scheduler.select(
            candidates,
            batch_size=settings.inference_batch_size,
            now=now,
        )
        selected = self.decode_selected(selected)
        if not selected:
            self.write_health(now=now, candidates=0, detections=0)
            return 0
        started = time.perf_counter()
        try:
            results = infer_batch(
                [candidate.frame for candidate in selected if candidate.frame is not None],
                weights=selected[0].weights,
                conf=0.25,
            )
        except Exception:
            self.errors += 1
            log.exception(
                "batch shadow inference failed cameras=%s",
                [candidate.camera_id for candidate in selected],
            )
            self.write_health(now=now, candidates=len(candidates), detections=0)
            return 0
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.latencies_ms.append(latency_ms)
        self.per_frame_latencies_ms.append(latency_ms / len(selected))
        self.batches += 1
        self.frames += len(selected)
        for candidate in selected:
            self.last_processed_ts[candidate.camera_id] = candidate.frame_ts
        detections = sum(len(result) for result in results)
        self.detections += detections
        self.write_health(
            now=now, candidates=len(candidates), detections=detections,
        )
        return len(selected)

    def write_health(self, *, now: float, candidates: int,
                     detections: int) -> None:
        if now - self.last_health_write < 1.0:
            return
        self.last_health_write = now
        active = set(self.specs)
        latencies = list(self.latencies_ms)
        per_frame_latencies = list(self.per_frame_latencies_ms)
        payload = {
            "mode": "shadow",
            "authoritative": False,
            "last_run_ts": now,
            "uptime_seconds": round(max(0.0, now - self.started), 1),
            "configured_cameras": len(active),
            "cameras_served": len(set(self.last_processed_ts) & active),
            "fresh_candidates": candidates,
            "batch_size_limit": int(settings.inference_batch_size),
            "batches_processed": self.batches,
            "frames_processed": self.frames,
            "detections_observed_not_emitted": self.detections,
            "errors": self.errors,
            "p50_batch_ms": round(statistics.median(latencies), 2) if latencies else None,
            "p95_batch_ms": (
                round(sorted(latencies)[
                    max(0, math.ceil(len(latencies) * 0.95) - 1)
                ], 2)
                if latencies else None
            ),
            "p95_per_frame_ms": (
                round(sorted(per_frame_latencies)[
                    max(0, math.ceil(len(per_frame_latencies) * 0.95) - 1)
                ], 2)
                if per_frame_latencies else None
            ),
            "max_camera_schedule_wait_seconds": round(
                self.scheduler.max_wait_seconds(active, now=now), 2,
            ),
        }
        self.redis.set(HEALTH_KEY, json.dumps(payload), ex=120)


def run() -> None:
    if not settings.inference_batch_shadow_enabled:
        raise RuntimeError("INFERENCE_BATCH_SHADOW_ENABLED must be true")
    if not settings.inference_batch_shadow_mode:
        raise RuntimeError(
            "authoritative batch mode is not implemented; shadow mode is required"
        )
    if settings.inference_batch_size > settings.inference_max_batch_size:
        raise RuntimeError(
            "INFERENCE_BATCH_SIZE cannot exceed INFERENCE_MAX_BATCH_SIZE"
        )
    env = HardwareEnv.detect()
    if settings.inference_batch_require_cuda and env.backend != "cuda":
        raise RuntimeError(
            f"batch shadow requires CUDA; detected backend={env.backend!r}"
        )
    log.info(
        "batch shadow starting backend=%s gpu=%s batch_size=%d",
        env.backend, env.gpu_name or "none", settings.inference_batch_size,
    )
    coordinator = BatchShadowCoordinator()
    while True:
        processed = coordinator.process_once()
        if not processed:
            time.sleep(0.1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
