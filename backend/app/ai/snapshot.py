"""Annotated alert snapshots.

When an operator-visible alert fires, we freeze the current camera
frame, draw a red box around the detection, stamp a human-readable
caption (alert type + time + store), and save it to disk. The path is
stored on detection_events.thumbnail_path so the Alerts page can show
the picture a non-technical manager actually needs.
"""
from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# Detectors that always warrant a saved snapshot — the ones a manager
# or guard needs to SEE, not just read.
SNAPSHOT_TYPES: set[str] = {
    "uniform_compliance", "intrusion", "shutter", "fight", "crowd",
    "trespass", "shrinkage", "fall", "abandoned_object", "weapon",
    "weapon_brandished", "staff_present", "staff_zone",
}

# Plain-English titles, mirrored on the frontend. Kept here so the
# stamped caption matches the card.
PLAIN_TITLES: dict[str, str] = {
    "uniform_compliance": "Staff Not in Uniform",
    "intrusion":          "Someone in Store After Hours",
    "shutter":            "Store Door Issue",
    "fight":              "Incident Detected",
    "crowd":              "Too Many People",
    "trespass":           "Unauthorised Person",
    "shrinkage":          "Suspicious Activity",
    "fall":               "Person May Have Fallen",
    "abandoned_object":   "Unattended Item",
    "weapon":             "Weapon Detected",
    "weapon_brandished":  "Weapon Detected",
    "staff_zone":         "Behind-counter Compliance",
}


def _snapshot_root() -> Path:
    p = Path(settings.recordings_dir) / "snapshots" / datetime.now().strftime("%Y-%m-%d")
    p.mkdir(parents=True, exist_ok=True)
    return p


# BGR colours for the per-person snapshot boxes.
_BOX_COLORS = {
    "green":  (0, 200, 0),     # staff
    "blue":   (235, 130, 0),   # customer
    "red":    (0, 0, 255),     # empty / detection
    "orange": (0, 165, 255),   # uniform-classified staff (preview signal)
}


def capture_alert_snapshot(frame_bgr, bbox_norm, alert_type: str,
                           camera_id: int, *, store_name: str | None = None,
                           camera_name: str | None = None,
                           boxes: list[dict] | None = None,
                           title_override: str | None = None) -> str | None:
    """Draw the box(es) + caption on a copy of `frame_bgr` and save it.
    Returns the on-disk path, or None if pixels/cv2 weren't available
    (best-effort — a missing snapshot must never block the alert).

    When `boxes` is given (list of {bbox, color, label}) we draw each
    one in its colour — green=staff, blue=customer, red=empty — instead
    of a single red box. Used by the counter-unattended alert so the
    manager sees exactly what the AI classified."""
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return None
    try:
        img = frame_bgr.copy()
        h, w = img.shape[:2]

        def _draw(bb, color_bgr, label=None):
            x1, y1, x2, y2 = bb
            p1 = (int(max(0, x1) * w), int(max(0, y1) * h))
            p2 = (int(min(1, x2) * w), int(min(1, y2) * h))
            if p2[0] > p1[0] and p2[1] > p1[1]:
                cv2.rectangle(img, p1, p2, color_bgr, 3)
                if label:
                    cv2.putText(img, label, (p1[0], max(12, p1[1] - 5)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color_bgr, 2, cv2.LINE_AA)

        def _annotate_sv(scene, box_list) -> bool:
            """P6: draw boxes via supervision annotators (cleaner boxes +
            labels; orange for staff-uniform). Returns True on success so the
            caller can fall back to the cv2 path on any failure."""
            try:
                import supervision as sv
                colors = {
                    "green":  sv.Color(r=0, g=200, b=0),
                    "blue":   sv.Color(r=0, g=130, b=235),
                    "red":    sv.Color(r=255, g=0, b=0),
                    "orange": sv.Color(r=255, g=165, b=0),   # staff uniform
                }
                for cname in {b.get("color", "red") for b in box_list}:
                    grp = [b for b in box_list
                           if b.get("color", "red") == cname
                           and b.get("bbox") and len(b["bbox"]) == 4]
                    if not grp:
                        continue
                    xyxy = np.array([[max(0, b["bbox"][0]) * w, max(0, b["bbox"][1]) * h,
                                      min(1, b["bbox"][2]) * w, min(1, b["bbox"][3]) * h]
                                     for b in grp], dtype=float)
                    dets = sv.Detections(xyxy=xyxy)
                    col = colors.get(cname, colors["red"])
                    scene = sv.BoxAnnotator(color=col, thickness=3).annotate(
                        scene=scene, detections=dets)
                    labels = [b.get("label", "") or "" for b in grp]
                    if any(labels):
                        scene = sv.LabelAnnotator(
                            color=col, text_color=sv.Color.WHITE).annotate(
                            scene=scene, detections=dets, labels=labels)
                return True
            except Exception:
                return False

        if boxes:
            if not _annotate_sv(img, boxes):
                for b in boxes:      # cv2 fallback
                    bb = b.get("bbox")
                    if bb and len(bb) == 4:
                        _draw(bb, _BOX_COLORS.get(b.get("color", "red"), (0, 0, 255)),
                              b.get("label"))
        elif bbox_norm and len(bbox_norm) == 4:
            _draw(bbox_norm, (0, 0, 255))

        # Caption banner along the top.
        title = title_override or PLAIN_TITLES.get(
            alert_type, alert_type.replace("_", " ").title())
        when = datetime.now().strftime("%I:%M %p  %a %d %b")
        loc = " | ".join(p for p in (store_name, camera_name) if p)
        caption = f"{title}   {when}"
        if loc:
            caption += f"   {loc}"
        # Semi-opaque dark strip so white text is always readable.
        strip_h = max(24, h // 18)
        overlay = img.copy()
        cv2.rectangle(overlay, (0, 0), (w, strip_h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)
        scale = max(0.5, min(1.0, w / 1280))
        cv2.putText(img, caption, (10, int(strip_h * 0.68)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255),
                    max(1, int(scale * 2)), cv2.LINE_AA)

        ts = datetime.now().strftime("%H%M%S_%f")
        path = _snapshot_root() / f"{camera_id}_{ts}.jpg"
        cv2.imwrite(str(path), img)
        return str(path)
    except Exception as e:
        log.warning("alert snapshot failed (cam %s, %s): %s",
                    camera_id, alert_type, e)
        return None
