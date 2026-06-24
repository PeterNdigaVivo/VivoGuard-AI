"""Save a preview sibling of a training-image crop with an orange
bounding box drawn on it.

Why two files: training samples (TrainingImage.file_path /
TrainingSample.frame_path) must NEVER carry the overlay — YOLO would
learn to detect orange boxes, not uniforms. The preview
(preview_path on both tables) is for human review only.

Best-effort throughout: any failure returns None and the caller
keeps the clean save + DB write going.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Keep in lock-step with snapshot._BOX_COLORS["orange"].
_ORANGE_BGR = (0, 165, 255)


def build_preview_path(file_path: str) -> str:
    """Sibling filename — same directory, `_preview` infix before .ext.
        /data/.../foo.jpg → /data/.../foo_preview.jpg"""
    p = Path(file_path)
    return str(p.with_name(f"{p.stem}_preview{p.suffix}"))


def write_preview(
    clean_path: str,
    label: str = "",
    bbox_norm: Optional[list] = None,
) -> Optional[str]:
    """Read the clean crop from disk, draw an orange bbox + optional
    label, write the preview alongside, return its path.
    Returns None on any failure (preview is best-effort — MUST NEVER
    block the clean save or DB write per Q1 confirmation).

    bbox_norm: [x1,y1,x2,y2] in normalised [0..1] image coords.
    When None we draw a full-frame orange border instead (the crop
    is already the person, so the rectangle wraps the whole image).

    label: drawn above the rectangle. Pass "" (default) for the
    no-text variant — used when uniform_color is absent (Q5: don't
    misleadingly write 'Staff' without evidence).
    """
    try:
        import cv2
    except ImportError:
        return None
    if not clean_path:
        return None
    try:
        img = cv2.imread(clean_path)
        if img is None:
            return None
        h, w = img.shape[:2]
        if bbox_norm and len(bbox_norm) == 4:
            x1, y1, x2, y2 = bbox_norm
            p1 = (int(max(0, x1) * w), int(max(0, y1) * h))
            p2 = (int(min(1, x2) * w), int(min(1, y2) * h))
        else:
            # Full-frame border (the crop IS the person).
            p1, p2 = (2, 2), (w - 2, h - 2)
        if p2[0] <= p1[0] or p2[1] <= p1[1]:
            return None
        cv2.rectangle(img, p1, p2, _ORANGE_BGR, 3)
        if label:
            cv2.putText(
                img, label, (p1[0], max(14, p1[1] - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, _ORANGE_BGR, 2, cv2.LINE_AA,
            )
        out = build_preview_path(clean_path)
        if not cv2.imwrite(out, img):
            return None
        return out
    except Exception as e:
        log.warning("preview write failed for %s: %s", clean_path, e)
        return None


def label_from_uniform_color(uniform_color: Optional[str]) -> str:
    """Map the detection-time uniform_color stamp to a preview label.
    Returns "" when no colour was identified — callers pass the
    empty string to write_preview() to skip text per Q5."""
    if uniform_color == "black":
        return "Black Uniform"
    if uniform_color == "maroon":
        return "Maroon Uniform"
    return ""
