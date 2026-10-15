"""Pure scoring rules for the model-evaluation report.

No I/O, no models: given what a model produced for one test sample, say
whether it was right. Kept separate so every rule is unit tested
(tests/test_eval_scoring.py) and identical for every model compared.
"""
from __future__ import annotations

import statistics
from collections import Counter

from app.ai.zone_logic import bbox_in_zone, bbox_overlaps_zone
from app.eval.categories import COUNTER_UNATTENDED, PERSON, UNIFORM

PERSON_CLASSES = frozenset({"person", "people", "human"})
# Uniform states that raise an alert (detectors/uniform_compliance.py).
UNIFORM_ALERTING_STATES = frozenset({"non_compliant", "partial_compliant", "color_only"})
# A click this close to the frame border counts as "edge of picture".
EDGE_MARGIN = 0.10


def person_boxes(detections: list[dict], conf_min: float = 0.25) -> list[list[float]]:
    """Normalised [x1,y1,x2,y2] boxes of people at or above `conf_min`."""
    return [d["bbox_norm"] for d in detections
            if str(d.get("cls", "")).lower() in PERSON_CLASSES
            and float(d.get("conf", 0.0)) >= conf_min]


def person_in_zone(boxes: list[list[float]], polygon: list | None, *,
                   generous: bool) -> bool:
    """Is anyone in the zone? `generous` = any overlap (counter presence,
    as the live counter rule does); otherwise the box centre must be
    inside. No polygon means the whole frame."""
    if not polygon:
        return bool(boxes)
    test = bbox_overlaps_zone if generous else bbox_in_zone
    return any(test(box, polygon) for box in boxes)


def model_raises_alert(category: str, *, person_present: bool | None = None,
                       uniform_state: str | None = None) -> bool:
    """Would the model's reading of this frame support the alert?"""
    if category == COUNTER_UNATTENDED:
        return not person_present                # unattended = nobody at the counter
    if category == PERSON:
        return bool(person_present)
    if category == UNIFORM:
        return uniform_state in UNIFORM_ALERTING_STATES
    raise ValueError(f"unknown category {category!r}")


def alert_outcome(verdict: str, raises_alert: bool) -> str:
    """Bucket one reviewed alert. `verdict` is the operator's: "true"
    (the alert was right) or "false"."""
    if verdict not in ("true", "false"):
        raise ValueError(f"verdict must be 'true' or 'false', got {verdict!r}")
    if verdict == "true":
        return "real_alert_kept" if raises_alert else "real_alert_missed"
    return "false_alert_repeated" if raises_alert else "false_alert_avoided"


def _contains(box: list[float], x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _is_edge(x: float, y: float) -> bool:
    return min(x, y, 1 - x, 1 - y) < EDGE_MARGIN


def match_clicks(boxes: list[list[float]], points: list[list[float]]) -> dict:
    """Match an admin's person clicks to detection boxes.

    Each click may claim one box that contains it (smallest box first, so
    overlapping people are not double-counted). Claimed boxes = people
    found; clicks without a box = missed people; boxes without a click =
    false detections."""
    by_area = sorted(range(len(boxes)),
                     key=lambda i: (boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1]))
    used: set[int] = set()
    found = missed = edge_total = edge_found = 0
    for x, y in points:
        edge = _is_edge(x, y)
        edge_total += edge
        hit = next((i for i in by_area if i not in used and _contains(boxes[i], x, y)), None)
        if hit is None:
            missed += 1
        else:
            used.add(hit)
            found += 1
            edge_found += edge
    return {"found": found, "missed": missed, "false": len(boxes) - len(used),
            "edge_total": edge_total, "edge_found": edge_found}


def summarise_alerts(outcomes: list[tuple[str, str]]) -> dict:
    """(category, outcome) pairs -> per-category counts and rates."""
    out: dict[str, dict] = {}
    for category in sorted({c for c, _ in outcomes}):
        counts = Counter(o for c, o in outcomes if c == category)
        real = counts["real_alert_kept"] + counts["real_alert_missed"]
        false = counts["false_alert_avoided"] + counts["false_alert_repeated"]
        out[category] = {
            "samples": real + false,
            "real_alert_kept": counts["real_alert_kept"],
            "real_alert_missed": counts["real_alert_missed"],
            "false_alert_avoided": counts["false_alert_avoided"],
            "false_alert_repeated": counts["false_alert_repeated"],
            "real_alerts_kept_pct": _pct(counts["real_alert_kept"], real),
            "false_alerts_avoided_pct": _pct(counts["false_alert_avoided"], false),
            "accuracy_pct": _pct(counts["real_alert_kept"] + counts["false_alert_avoided"],
                                 real + false),
        }
    return out


def summarise_people(matches: list[dict]) -> dict:
    total = {k: sum(m[k] for m in matches)
             for k in ("found", "missed", "false", "edge_total", "edge_found")}
    people = total["found"] + total["missed"]
    return {
        "frames": len(matches),
        "people_labelled": people,
        "people_found": total["found"],
        "people_missed": total["missed"],
        "false_detections": total["false"],
        "found_pct": _pct(total["found"], people),
        "precision_pct": _pct(total["found"], total["found"] + total["false"]),
        "edge_people": total["edge_total"],
        "edge_found_pct": _pct(total["edge_found"], total["edge_total"]),
    }


def summarise_speed(ms: list[float]) -> dict:
    if not ms:
        return {"frames": 0, "median_ms": None, "p95_ms": None}
    ordered = sorted(ms)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"frames": len(ms), "median_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(p95, 2)}


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None
