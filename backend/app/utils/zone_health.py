"""Per-store zone-health scoring.

Pure function — no DB, no HTTP, easy to unit-test. Caller fetches the
chain's zones + cameras once and feeds the rows in.

Rubric (per Stephen's Feature 4 spec):

  100-point ceiling. A store needs ALL FOUR required zone tags
  across its 4 cameras (not necessarily on ONE camera):

    entry_exit  (footfall counting)         +25
    queue       (checkout queue)            +25
    staff_zone  (staff area / counter cov)  +25
    shutter     (shop-open shutter sensor)  +25

  Penalties:
    duplicate zones on the SAME camera     −10 per duplicate
    typo'd zone name                        −5  per zone
    camera offline                         −15 per camera

  Note on duplicates: two cameras in a 4-camera store each having
  ONE entry_exit zone is intentional coverage — NOT a duplicate.
  A duplicate is only when one camera carries 2+ zones with the
  same tag (the audit's "Cam 14 two entry_exit" / "Cam 152 three
  entry_exit" cases).

Empty-store edge case: a store with zero cameras returns score 0
and the "no_cameras" failure reason — not a crash, not null.
"""
from __future__ import annotations
import difflib
import re
from typing import Iterable

from app.zone_purposes import ZONE_PURPOSES   # noqa: F401  (kept for ref)


REQUIRED_TAGS:       tuple[str, ...] = ("entry_exit", "queue", "staff_zone", "shutter")
POINTS_PER_REQUIRED: int = 25
PENALTY_DUPLICATE:   int = 10
PENALTY_TYPO:        int = 5
PENALTY_OFFLINE_CAM: int = 15

# Healthy / Needs Attention / Critical thresholds.
BADGE_HEALTHY_MIN:  int = 90
BADGE_WARN_MIN:     int = 60

# difflib similarity threshold for typo detection. Empirically picked
# so the audit's known word-level typos all flag (Checkuout / conter /
# Shinkage all score ≥ 0.75 against their canonical keyword) while
# correctly-spelled operator names don't. Tunable; the similarity
# score is returned alongside each typo finding so we can lower /
# raise it later from real-data noise.
TYPO_SIMILARITY_MIN: float = 0.6

# Word-level typo detection. The audit cases are all subtle letter-
# swaps of a single keyword inside the zone name ("Checkuout",
# "conter", "Shinkage"). Comparing against the FULL canonical label
# ("Monitor checkout queue") was wrong — operators legitimately
# shorten names. Match each non-stopword in the zone name against
# this fixed keyword list instead.
TYPO_KEYWORDS: tuple[str, ...] = (
    "checkout", "counter", "shrinkage", "shutter", "queue",
    "staff",    "entrance", "footfall",  "aisle",   "stockroom",
)
# Common nouns / verbs operators use in zone names that we should
# NEVER flag as typos — they're descriptive, not detector-keywords.
TYPO_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "at", "to", "for", "and", "or", "by",
    "monitor", "count", "people", "entering", "watch", "track", "flag",
    "behind", "area", "zone", "front", "door", "side", "back", "main",
    "left", "right", "store", "shop", "till", "near", "next", "with",
    "from", "into", "during", "after", "before", "hours", "afterhours",
    "sensor", "camera", "view", "point", "line", "exit",
})


def _detect_typo(zone_name: str,
                  _canonical_unused: list[str] | None = None
                  ) -> tuple[bool, str | None, float]:
    """Word-level typo detector.

    Returns (is_typo, suggested_full_name, similarity).
      • is_typo: True when some non-stopword word in `zone_name` is
        close to a TYPO_KEYWORDS entry but not exactly one of them.
      • suggested_full_name: zone_name with the typo word replaced by
        the canonical keyword, capitalised to match the original word
        (so "Checkuout" → "Checkout", "conter" → "counter").
      • similarity: the matched word's similarity to its keyword.

    The `_canonical_unused` parameter is kept for API stability with
    the earlier draft; current logic uses TYPO_KEYWORDS directly.
    """
    if not zone_name:
        return False, None, 0.0
    words = re.findall(r"[A-Za-z]+", zone_name)
    for original in words:
        lower = original.lower()
        if lower in TYPO_STOP_WORDS:
            continue
        if lower in TYPO_KEYWORDS:
            continue   # spelled correctly
        best_kw, best_ratio = None, 0.0
        for kw in TYPO_KEYWORDS:
            ratio = difflib.SequenceMatcher(None, lower, kw).ratio()
            if ratio > best_ratio:
                best_kw, best_ratio = kw, ratio
        if best_kw and best_ratio >= TYPO_SIMILARITY_MIN:
            # Preserve the original word's casing on the suggestion.
            if   original.isupper():    fixed = best_kw.upper()
            elif original[0].isupper(): fixed = best_kw.capitalize()
            else:                       fixed = best_kw
            suggested = zone_name.replace(original, fixed, 1)
            return True, suggested, best_ratio
    return False, None, 0.0


def _canonical_zone_labels() -> list[str]:
    """Operator-facing labels exposed by ZONE_PURPOSES — kept for
    callers / tests that want to verify a candidate against the
    dropdown's known options."""
    return [v["label"] for v in ZONE_PURPOSES.values()]


def _badge(score: int) -> str:
    if score >= BADGE_HEALTHY_MIN: return "healthy"
    if score >= BADGE_WARN_MIN:    return "warn"
    return "critical"


def score_store(
    store_id: int,
    store_name: str | None,
    cameras: Iterable[dict],
    zones: Iterable[dict],
) -> dict:
    """Compute the health dict for one store.

    cameras: iterable of {id, name, status}
    zones:   iterable of {id, camera_id, name, detection_types_json[]}

    Returns the full payload the dashboard renders — score, badge,
    per-tag-present map, list of issues (each with a kind + display
    + actionable fields like rename-suggestion).
    """
    cams = list(cameras)
    if not cams:
        return {
            "store_id":   store_id,
            "store_name": store_name,
            "score":      0,
            "badge":      "critical",
            "per_tag_present": {t: False for t in REQUIRED_TAGS},
            "cameras_total":   0,
            "cameras_online":  0,
            "cameras_offline": 0,
            "issues": [{"kind": "no_cameras",
                         "message": "No cameras configured"}],
        }

    zone_rows = list(zones)

    # Per-tag presence — does ANY camera in the store have ANY zone
    # carrying this tag?
    per_tag_present: dict[str, bool] = {t: False for t in REQUIRED_TAGS}
    for z in zone_rows:
        tags = set(z.get("detection_types_json") or [])
        for t in REQUIRED_TAGS:
            if t in tags:
                per_tag_present[t] = True

    score = sum(POINTS_PER_REQUIRED for t in REQUIRED_TAGS if per_tag_present[t])

    issues: list[dict] = []

    # Missing required tags.
    for t in REQUIRED_TAGS:
        if not per_tag_present[t]:
            issues.append({
                "kind":      "missing_tag",
                "tag":       t,
                "message":   f"No camera has a {t} zone",
                "deduction": 0,    # already costed via the +25 not awarded
            })

    # Per-camera duplicates. A duplicate is more than one zone with
    # the same tag on the SAME camera (the audit's actual bug, not
    # coverage across cameras).
    zones_by_cam: dict[int, list[dict]] = {}
    for z in zone_rows:
        zones_by_cam.setdefault(int(z["camera_id"]), []).append(z)
    cam_name_by_id = {int(c["id"]): c.get("name") for c in cams}
    for cam_id, czs in zones_by_cam.items():
        tag_counts: dict[str, int] = {}
        for z in czs:
            for t in (z.get("detection_types_json") or []):
                if t in REQUIRED_TAGS:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
        for t, n in tag_counts.items():
            extras = n - 1
            if extras > 0:
                deduction = extras * PENALTY_DUPLICATE
                score -= deduction
                issues.append({
                    "kind":      "duplicate_zone",
                    "tag":       t,
                    "camera_id": cam_id,
                    "camera_name": cam_name_by_id.get(cam_id),
                    "count":     n,
                    "message":   (f"Camera {cam_name_by_id.get(cam_id) or cam_id} "
                                   f"has {n} {t} zones (should be 1)"),
                    "deduction": deduction,
                })

    # Typos — word-level keyword match (see _detect_typo).
    for z in zone_rows:
        is_typo, suggestion, similarity = _detect_typo(
            z.get("name") or "")
        if is_typo:
            score -= PENALTY_TYPO
            issues.append({
                "kind":              "typo_name",
                "zone_id":           z.get("id"),
                "camera_id":         z.get("camera_id"),
                "camera_name":       cam_name_by_id.get(int(z["camera_id"])),
                "current_name":      z.get("name"),
                "suggested_name":    suggestion,
                "similarity":        round(similarity, 3),
                "message":           (f"Zone name “{z.get('name')}” looks like "
                                       f"a typo of “{suggestion}”"),
                "deduction":         PENALTY_TYPO,
            })

    # Offline cameras.
    cameras_online  = 0
    cameras_offline = 0
    for c in cams:
        st = (c.get("status") or "").lower()
        if st == "offline":
            cameras_offline += 1
            score -= PENALTY_OFFLINE_CAM
            issues.append({
                "kind":        "camera_offline",
                "camera_id":   c.get("id"),
                "camera_name": c.get("name"),
                "message":     f"Camera {c.get('name') or c.get('id')} is offline",
                "deduction":   PENALTY_OFFLINE_CAM,
            })
        elif st == "online":
            cameras_online += 1
        # pending / degraded / unknown counted as neither online nor offline

    # Clamp to [0, 100] — multiple deductions can drive the raw score
    # negative; the badge bucketing only cares about ≥0.
    score = max(0, min(100, score))

    return {
        "store_id":        store_id,
        "store_name":      store_name,
        "score":           score,
        "badge":           _badge(score),
        "per_tag_present": per_tag_present,
        "cameras_total":   len(cams),
        "cameras_online":  cameras_online,
        "cameras_offline": cameras_offline,
        "issues":          issues,
    }
