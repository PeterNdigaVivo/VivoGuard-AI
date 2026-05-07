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
