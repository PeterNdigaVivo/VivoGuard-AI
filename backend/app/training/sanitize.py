"""Dataset sanitization pre-check — runs before every training job.

Operates on a staged YOLO dataset root (the directory `dataset_root()`
returns), i.e. the layout `write_yolo_dataset_yaml` produces:

    ds_<id>/
      train.txt / val.txt / test.txt   ← manifests of absolute image paths
      images/<img.id><ext>
      labels/<img.id>.txt              ← YOLO labels (empty = background)

Checks, in order:
  1. corrupt   — images cv2 cannot decode are excluded.
  2. duplicate — SHA256 over file bytes; the FIRST occurrence (manifest
                 order, train before val before test) is kept, later
                 copies are excluded so the same frame can never sit in
                 both train and val (a silent metric-inflation bug).
  3. blur      — variance of the Laplacian below `blur_threshold` marks
                 an image as severely blurred and excludes it.

Exclusion REWRITES the manifests (train/val/test.txt) minus the excluded
paths — no image or label file is deleted, so the operation is reversible
by re-running `write_yolo_dataset_yaml`.

The returned report also includes the class distribution parsed from the
surviving label files, with imbalance warnings, so extreme skew is
surfaced BEFORE compute is spent on a doomed run.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFESTS: tuple[str, ...] = ("train", "val", "test")

# Variance-of-Laplacian below this = severely blurred. Conservative on
# purpose: 100+ is a common "sharp" bar; we only exclude images that are
# unusable for gradient signal, not merely soft CCTV frames.
DEFAULT_BLUR_THRESHOLD: float = 25.0

# Class-distribution warnings.
IMBALANCE_RATIO: float = 10.0        # max/min instance ratio that warns
MIN_CLASS_INSTANCES: int = 5         # classes below this warn


def _laplacian_variance(path: Path) -> float | None:
    """Variance of the Laplacian of the greyscale image; None when the
    file cannot be decoded (caller treats that as corrupt)."""
    import cv2                       # heavy import kept out of module load
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _label_path_for(image_path: Path, root: Path) -> Path:
    """YOLO's images/ -> labels/ path swap for a staged image."""
    return (root / "labels" / image_path.name).with_suffix(".txt")


def _class_names(root: Path) -> list[str]:
    """Parse `names: [...]` from data.yaml (written by
    write_yolo_dataset_yaml as a python-list literal)."""
    yaml_path = root / "data.yaml"
    if not yaml_path.exists():
        return []
    for line in yaml_path.read_text().splitlines():
        if line.startswith("names:"):
            try:
                import ast
                val = ast.literal_eval(line.split(":", 1)[1].strip())
                return [str(v) for v in val] if isinstance(val, list) else []
            except (ValueError, SyntaxError):
                return []
    return []


def sanitize_dataset(dataset_path: str, *,
                     blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
                     apply: bool = True) -> dict[str, Any]:
    """Sanitize a staged YOLO dataset before training.

    Args:
        dataset_path:   dataset root containing train/val/test.txt.
        blur_threshold: variance-of-Laplacian floor; below = excluded.
        apply:          when True (default) the manifests are rewritten
                        minus excluded images; False = report-only.

    Returns a report dict:
        total_images, kept, per-manifest kept counts,
        excluded_corrupt / excluded_duplicate / excluded_blurred (paths),
        class_distribution, background_images, imbalance_warnings.

    Raises:
        ValueError:   dataset root or train.txt manifest missing.
        RuntimeError: OpenCV unavailable (required for decode/blur).
    """
    root = Path(dataset_path)
    if not root.is_dir():
        raise ValueError(f"dataset root does not exist: {root}")
    if not (root / "train.txt").exists():
        raise ValueError(f"no train.txt manifest under {root} — run "
                         f"write_yolo_dataset_yaml first")
    import importlib.util
    if importlib.util.find_spec("cv2") is None:             # pragma: no cover
        raise RuntimeError(
            "sanitize_dataset requires OpenCV (cv2) for image decoding "
            "and blur scoring")

    manifests: dict[str, list[str]] = {}
    for name in MANIFESTS:
        p = root / f"{name}.txt"
        manifests[name] = (
            [ln for ln in p.read_text().splitlines() if ln.strip()]
            if p.exists() else [])

    seen_hashes: dict[str, str] = {}                # sha256 → first path
    excluded_corrupt: list[str] = []
    excluded_duplicate: list[str] = []
    excluded_blurred: list[str] = []
    kept: dict[str, list[str]] = {name: [] for name in MANIFESTS}

    total = 0
    for name in MANIFESTS:                          # train first: dup
        for entry in manifests[name]:               # priority favours train
            total += 1
            path = Path(entry)
            if not path.exists():
                excluded_corrupt.append(entry)      # missing = unusable
                continue
            digest = _sha256(path)
            if digest in seen_hashes:
                excluded_duplicate.append(entry)
                continue
            variance = _laplacian_variance(path)
            if variance is None:
                excluded_corrupt.append(entry)
                continue
            if variance < blur_threshold:
                excluded_blurred.append(entry)
                continue
            seen_hashes[digest] = entry
            kept[name].append(entry)

    # Class distribution over surviving labels.
    names = _class_names(root)
    counts: dict[str, int] = {}
    background = 0
    for name in MANIFESTS:
        for entry in kept[name]:
            lp = _label_path_for(Path(entry), root)
            if not lp.exists():
                continue
            lines = [ln for ln in lp.read_text().splitlines() if ln.strip()]
            if not lines:
                background += 1
                continue
            for ln in lines:
                try:
                    cls_id = int(ln.split()[0])
                except (ValueError, IndexError):
                    continue
                label = (names[cls_id] if 0 <= cls_id < len(names)
                         else f"class_{cls_id}")
                counts[label] = counts.get(label, 0) + 1

    warnings: list[str] = []
    if counts:
        mx, mn = max(counts.values()), min(counts.values())
        if mn and mx / mn > IMBALANCE_RATIO:
            warnings.append(
                f"extreme class imbalance: max/min instance ratio "
                f"{mx / mn:.1f} > {IMBALANCE_RATIO} ({counts})")
        for label, n in sorted(counts.items()):
            if n < MIN_CLASS_INSTANCES:
                warnings.append(
                    f"class '{label}' has only {n} instances "
                    f"(< {MIN_CLASS_INSTANCES}) — metrics for it will be noise")

    if apply:
        for name in MANIFESTS:
            p = root / f"{name}.txt"
            if p.exists() or kept[name]:
                p.write_text("\n".join(kept[name]))

    report: dict[str, Any] = {
        "dataset_path":        str(root),
        "total_images":        total,
        "kept":                sum(len(v) for v in kept.values()),
        "kept_per_split":      {k: len(v) for k, v in kept.items()},
        "excluded_corrupt":    excluded_corrupt,
        "excluded_duplicate":  excluded_duplicate,
        "excluded_blurred":    excluded_blurred,
        "class_distribution":  counts,
        "background_images":   background,
        "imbalance_warnings":  warnings,
        "blur_threshold":      blur_threshold,
        "applied":             apply,
    }
    log.info("sanitize_dataset %s: total=%d kept=%d corrupt=%d dup=%d "
             "blurred=%d classes=%s warnings=%d",
             root, total, report["kept"], len(excluded_corrupt),
             len(excluded_duplicate), len(excluded_blurred), counts,
             len(warnings))
    return report
