"""Zone-related geometry: point-in-polygon, line crossing, IOU.

All zone coords are normalised to [0..1] image space so they're
resolution-independent.
"""
from __future__ import annotations


def point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    """Ray-casting. `polygon` is a list of [x, y] vertex pairs."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def bbox_centre(bbox: list[float]) -> tuple[float, float]:
    """`bbox` is [x1, y1, x2, y2]; returns the centre point."""
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def bbox_in_zone(bbox_norm: list[float], polygon: list[list[float]]) -> bool:
    """True if the bbox centre lies inside the polygon."""
    cx, cy = bbox_centre(bbox_norm)
    return point_in_polygon(cx, cy, polygon)


def bbox_overlaps_zone(bbox_norm: list[float],
                       polygon: list[list[float]]) -> bool:
    """True if the person bbox OVERLAPS the polygon at all (not just its
    foot-point). Deliberately generous — used for counter presence so a
    customer leaning over / standing at the counter still counts as
    'someone is there'. Checks any bbox corner inside the polygon, any
    polygon vertex inside the bbox, or the bbox centre inside the polygon."""
    x1, y1, x2, y2 = bbox_norm
    if any(point_in_polygon(px, py, polygon)
           for px, py in ((x1, y1), (x2, y1), (x1, y2), (x2, y2))):
        return True
    for px, py in polygon:
        if x1 <= px <= x2 and y1 <= py <= y2:
            return True
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    return point_in_polygon(cx, cy, polygon)


def iou(a: list[float], b: list[float]) -> float:
    """IOU of two [x1,y1,x2,y2] boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = (ax2 - ax1) * (ay2 - ay1)
    bb = (bx2 - bx1) * (by2 - by1)
    return inter / (aa + bb - inter)


def segments_cross(p1: tuple[float, float], p2: tuple[float, float],
                   p3: tuple[float, float], p4: tuple[float, float]) -> bool:
    """True if line segment p1→p2 crosses segment p3→p4."""
    def ccw(a, b, c) -> bool:
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


# ── Supervision PolygonZone (P4) ───────────────────────────────────────────
# Optional, more-accurate zone containment via sv.PolygonZone. Opt-in: callers
# pass zone_id + frame_wh to use it; everything else keeps calling
# bbox_in_zone() (centroid) unchanged. PolygonZone tests the detection's
# ANCHOR (bottom-center / foot-point) against the polygon — more correct for
# people than the centroid, but a behaviour change, so adoption is per-caller.
#
# Cache keyed by (zone_id, w, h) AND the polygon contents, so a re-drawn zone
# rebuilds automatically (there is no global zone-update signal to hook).
_polyzone_cache: dict = {}


def zone_contains(bbox_norm: list[float], polygon: list[list[float]], *,
                  zone_id: int | None = None,
                  frame_wh: tuple[int, int] | None = None) -> bool:
    """True if the detection is inside the polygon. Uses sv.PolygonZone
    (foot-point anchor) when supervision + zone_id + frame_wh are all
    available; otherwise falls back to the centroid test. Never raises."""
    if zone_id is None or not frame_wh or not polygon:
        return bbox_in_zone(bbox_norm, polygon)
    try:
        import numpy as np
        import supervision as sv
        w, h = int(frame_wh[0]), int(frame_wh[1])
        if w <= 0 or h <= 0:
            return bbox_in_zone(bbox_norm, polygon)
        poly_key = tuple(tuple(round(float(c), 5) for c in pt) for pt in polygon)
        ck = (int(zone_id), w, h)
        cached = _polyzone_cache.get(ck)
        if cached is None or cached[0] != poly_key:
            pts = np.array([[p[0] * w, p[1] * h] for p in polygon], dtype=np.int64)
            pz = sv.PolygonZone(polygon=pts)
            _polyzone_cache[ck] = (poly_key, pz)
        else:
            pz = cached[1]
        x1, y1, x2, y2 = bbox_norm
        dets = sv.Detections(xyxy=np.array(
            [[x1 * w, y1 * h, x2 * w, y2 * h]], dtype=float))
        mask = pz.trigger(dets)
        return bool(mask[0]) if len(mask) else False
    except Exception:
        # supervision missing / API drift / bad polygon → safe centroid path.
        return bbox_in_zone(bbox_norm, polygon)
