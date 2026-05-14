"""Fight / altercation detector — tracker-based heuristic.

This is not a fight-trained CNN — it's a movement-pattern rule:
  - ≥ 2 person tracks within `proximity_norm` (default 0.15 of frame
    width/height) for at least `dwell_frames` consecutive frames,
  - average velocity magnitude across involved tracks ≥ `velocity_norm`
    (default 0.06 normalised coords per frame),
  - => fire a high-priority 'fight' alert.

False positives include crowded queues with fast movement (kids,
shopping bags). Operators dial it with the two extras knobs. For better
recall a temporal action-recognition model (X3D, SlowFast) can replace
this — feed its 'fight' class label through CustomDetector with
class_set={'fight'}.

Tags: no zone required — fires globally. Tag a zone 'fight_arena'
(optional) to constrain it spatially.
"""
from __future__ import annotations
import time
from math import hypot

from app.ai.detectors.base import (
    COCO_PERSON, Detector, DetectorContext, DetectionEvent,
)
from app.ai.zone_logic import bbox_centre, bbox_in_zone


class FightDetector(Detector):
    detection_type = "fight"
    needs_tracking = True

    def __init__(self):
        # track_id → (last_cx, last_cy, last_t)
        self._last_pos: dict[int, tuple[float, float, float]] = {}
        # cluster_key → consecutive-frame count
        self._cluster_streak: dict[tuple[int, ...], int] = {}
        self._fired_at: float = 0.0

    def evaluate(self, ctx: DetectorContext) -> list[DetectionEvent]:
        cfg = ctx.config.get(self.detection_type)
        if not cfg or not cfg.get("enabled"):
            return []
        extra        = cfg.get("extra") or {}
        proximity    = float(extra.get("proximity_norm", 0.15))
        velocity_min = float(extra.get("velocity_norm", 0.06))
        dwell_frames = int(extra.get("dwell_frames", 4))
        cluster_min  = int(extra.get("min_people", 2))

        # Optional spatial constraint.
        arenas = [z for z in ctx.zones if "fight_arena" in (z.get("detection_types_json") or [])]

        # Compute per-track position + velocity this frame.
        now = time.time()
        people = []
        for tr, _ in ctx.tracks:
            if tr.cls not in COCO_PERSON:
                continue
            cx, cy = bbox_centre(tr.bbox_norm)
            if arenas and not any(bbox_in_zone(tr.bbox_norm, a["polygon_coords_json"]) for a in arenas):
                continue
            prev = self._last_pos.get(tr.track_id)
            self._last_pos[tr.track_id] = (cx, cy, now)
            if prev is None:
                continue
            vx, vy = cx - prev[0], cy - prev[1]
            v = hypot(vx, vy)
            people.append((tr.track_id, cx, cy, v, tr.bbox_norm))

        # Find clusters: any pair within proximity contributes to a cluster.
        # Simple union-find with one pass.
        n = len(people)
        if n < cluster_min:
            return []
        parent = list(range(n))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def union(i, j):
            parent[find(i)] = find(j)
        for i in range(n):
            for j in range(i + 1, n):
                if hypot(people[i][1] - people[j][1], people[i][2] - people[j][2]) <= proximity:
                    union(i, j)
        clusters: dict[int, list[int]] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(i)

        out: list[DetectionEvent] = []
        for members in clusters.values():
            if len(members) < cluster_min:
                continue
            avg_v = sum(people[i][3] for i in members) / len(members)
            if avg_v < velocity_min:
                continue
            key = tuple(sorted(people[i][0] for i in members))
            self._cluster_streak[key] = self._cluster_streak.get(key, 0) + 1
            if self._cluster_streak[key] < dwell_frames:
                continue
            if now - self._fired_at < 30:
                continue
            self._fired_at = now
            # Bounding box covering all members.
            xs = [people[i][4][0] for i in members] + [people[i][4][2] for i in members]
            ys = [people[i][4][1] for i in members] + [people[i][4][3] for i in members]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
            out.append(DetectionEvent(
                detection_type=self.detection_type, cls="fight",
                confidence=min(1.0, avg_v / max(velocity_min, 1e-6)),
                bbox_norm=bbox,
                extra={"priority": "high", "people": len(members),
                       "avg_velocity": round(avg_v, 3),
                       "track_ids": list(key), "store_id": ctx.store_id},
            ))
        # Reap stale streaks.
        for k in list(self._cluster_streak):
            if all(tid not in self._last_pos for tid in k):
                self._cluster_streak.pop(k, None)
        return out
