"""Cross-camera person re-identification tracker (Sprint 2.2).

Keeps a per-store gallery of recent appearance embeddings in Redis so a
person seen on camera A ("Entrance") can be matched to the same person on
camera B ("Counter") minutes later. That single global identity is what
lets us:

  * count UNIQUE visitors instead of per-camera appearances (the same
    shopper crossing 3 cameras was being counted 3x),
  * draw customer journeys (Entrance -> Aisle -> Counter -> Exit),
  * follow a flagged person across the store.

State lives entirely in Redis (shared across the per-camera worker
processes), keyed per store:

    vg:reid:gallery:{store_id}   HASH  global_id -> JSON {
        "e":         [512 floats],   # L2-normalized embedding (EWMA-smoothed)
        "camera_id": int,            # last camera that matched
        "last_seen": float,          # epoch seconds
        "bbox":      [x1,y1,x2,y2],  # last pixel bbox (debug/journey)
        "n":         int,            # times matched (confidence proxy)
    }

Retention: each entry carries `last_seen`; entries older than the TTL are
lazily evicted on every match/register, and a whole-key EXPIRE is
refreshed on each touch so an idle store's gallery disappears on its own.
A soft size cap evicts the oldest entries so a busy store can't grow the
gallery without bound.

Like the encoder, this is BEST-EFFORT: any Redis hiccup or malformed
entry is swallowed and degrades Re-ID to "register everything as new"
(i.e. behaviour reverts to the old per-camera counting) rather than
breaking inference.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

import numpy as np

from app.config import settings

log = logging.getLogger(__name__)


def _gallery_key(store_id: int) -> str:
    return f"vg:reid:gallery:{store_id}"


class CrossCameraTracker:
    """Match/register appearance embeddings against a per-store gallery.

    `redis_client` is a redis-py client (the same `pub` connection the
    inference loop already holds). Thresholds/TTL come from settings but
    can be overridden per instance for tests.
    """

    def __init__(self, redis_client, *,
                 threshold: float | None = None,
                 ttl_seconds: int | None = None,
                 max_gallery_size: int | None = None,
                 ewma_alpha: float = 0.3) -> None:
        self.r = redis_client
        self.threshold = (settings.reid_match_threshold
                          if threshold is None else threshold)
        self.ttl = (settings.reid_gallery_ttl_seconds
                    if ttl_seconds is None else ttl_seconds)
        self.max_size = (settings.reid_max_gallery_size
                         if max_gallery_size is None else max_gallery_size)
        # EWMA weight for blending a fresh embedding into the stored one.
        # Low alpha = the gallery vector drifts slowly, robust to a single
        # bad crop.
        self.alpha = ewma_alpha

    # -- gallery IO ---------------------------------------------------
    def _load_gallery(self, store_id: int) -> dict[str, dict]:
        """Return {global_id: entry} for a store, dropping expired/garbled rows."""
        try:
            raw = self.r.hgetall(_gallery_key(store_id))
        except Exception as e:                          # noqa: BLE001
            log.debug("reid gallery read failed store=%s: %s", store_id, e)
            return {}
        out: dict[str, dict] = {}
        now = time.time()
        for k, v in (raw or {}).items():
            gid = k.decode() if isinstance(k, (bytes, bytearray)) else k
            try:
                entry = json.loads(v)
                if now - float(entry.get("last_seen", 0)) > self.ttl:
                    continue                            # expired — skip (lazy evict below)
                out[gid] = entry
            except Exception:                           # noqa: BLE001
                continue
        return out

    def _prune(self, store_id: int, gallery: dict[str, dict]) -> None:
        """Evict expired entries and enforce the size cap (oldest-first)."""
        try:
            key = _gallery_key(store_id)
            now = time.time()
            # 1. drop anything past TTL.
            try:
                raw = self.r.hgetall(key)
            except Exception:                           # noqa: BLE001
                raw = {}
            expired = []
            for k, v in (raw or {}).items():
                gid = k.decode() if isinstance(k, (bytes, bytearray)) else k
                try:
                    if now - float(json.loads(v).get("last_seen", 0)) > self.ttl:
                        expired.append(gid)
                except Exception:                       # noqa: BLE001
                    expired.append(gid)                 # garbled -> evict
            if expired:
                self.r.hdel(key, *expired)
            # 2. size cap — evict oldest last_seen beyond max_size.
            if len(gallery) > self.max_size:
                ordered = sorted(gallery.items(),
                                 key=lambda kv: kv[1].get("last_seen", 0))
                drop = [gid for gid, _ in ordered[: len(gallery) - self.max_size]]
                if drop:
                    self.r.hdel(key, *drop)
        except Exception as e:                          # noqa: BLE001
            log.debug("reid prune failed store=%s: %s", store_id, e)

    def _write(self, store_id: int, global_id: str, entry: dict) -> None:
        try:
            key = _gallery_key(store_id)
            self.r.hset(key, global_id, json.dumps(entry))
            # Refresh whole-key TTL so an idle store self-cleans. Pad past
            # the per-entry TTL so lazy eviction handles the fine-grained
            # expiry while this guarantees ultimate cleanup.
            self.r.expire(key, int(self.ttl) + 600)
        except Exception as e:                          # noqa: BLE001
            log.debug("reid gallery write failed store=%s: %s", store_id, e)

    # -- matching -----------------------------------------------------
    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        # Both vectors are already L2-normalized, so the dot product is
        # the cosine similarity.
        return float(np.dot(a, b))

    def match(self, embedding: np.ndarray, store_id: int,
              *, gallery: dict[str, dict] | None = None) -> tuple[str | None, float]:
        """Return (global_id, score) of the best gallery match, or (None, best_score).

        A match is accepted only when score >= self.threshold.
        """
        if embedding is None:
            return None, 0.0
        gal = self._load_gallery(store_id) if gallery is None else gallery
        best_id, best_score = None, 0.0
        for gid, entry in gal.items():
            try:
                vec = np.asarray(entry["e"], dtype=np.float32)
            except Exception:                           # noqa: BLE001
                continue
            if vec.shape[0] != embedding.shape[0]:
                continue
            score = self._cosine(embedding, vec)
            if score > best_score:
                best_id, best_score = gid, score
        if best_id is not None and best_score >= self.threshold:
            return best_id, best_score
        return None, best_score

    def register(self, embedding: np.ndarray, store_id: int, camera_id: int,
                 *, bbox: list[int] | None = None) -> str | None:
        """Insert a brand-new identity and return its global_id."""
        if embedding is None:
            return None
        global_id = f"vg_reid_{store_id}_{uuid.uuid4().hex[:8]}"
        entry = {
            "e": [float(x) for x in embedding.tolist()],
            "camera_id": int(camera_id),
            "last_seen": time.time(),
            "bbox": bbox or [],
            "n": 1,
        }
        self._write(store_id, global_id, entry)
        return global_id

    def match_or_register(self, embedding: np.ndarray, store_id: int,
                          camera_id: int, *, bbox: list[int] | None = None) -> str | None:
        """The everyday entry point used by the inference loop.

        Matches against the store gallery; on a hit, EWMA-updates the
        stored embedding + last_seen and returns the existing global_id.
        On a miss, registers a fresh identity. Prunes the gallery as a
        side effect. Returns None only on total failure (never raises).
        """
        if embedding is None:
            return None
        try:
            gallery = self._load_gallery(store_id)
            gid, score = self.match(embedding, store_id, gallery=gallery)
            if gid is not None:
                entry = gallery.get(gid, {})
                try:
                    prev = np.asarray(entry.get("e"), dtype=np.float32)
                    blended = (1.0 - self.alpha) * prev + self.alpha * embedding
                    n = float(np.linalg.norm(blended))
                    smoothed = (blended / n) if n > 1e-6 else embedding
                except Exception:                       # noqa: BLE001
                    smoothed = embedding
                entry["e"] = [float(x) for x in np.asarray(smoothed).tolist()]
                entry["camera_id"] = int(camera_id)
                entry["last_seen"] = time.time()
                if bbox:
                    entry["bbox"] = bbox
                entry["n"] = int(entry.get("n", 1)) + 1
                self._write(store_id, gid, entry)
                self._prune(store_id, gallery)
                return gid
            # No match -> new identity.
            new_id = self.register(embedding, store_id, camera_id, bbox=bbox)
            self._prune(store_id, gallery)
            return new_id
        except Exception as e:                          # noqa: BLE001
            log.debug("reid match_or_register failed store=%s: %s", store_id, e)
            return None
