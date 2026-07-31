"""Tests for app.training.sanitize.sanitize_dataset.

Builds a real staged-dataset directory (cv2-encoded JPEGs + YOLO labels
+ manifests + data.yaml) in tmp_path and asserts the three exclusion
paths (corrupt / duplicate / blurred), the class-distribution report,
imbalance warnings, and the manifest-rewrite semantics.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.training.sanitize import (          # noqa: E402
    DEFAULT_BLUR_THRESHOLD, sanitize_dataset,
)


def _write_jpg(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), img)


def _sharp(seed: int) -> np.ndarray:
    """High-frequency noise — Laplacian variance far above threshold."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (96, 96, 3), dtype=np.uint8)


def _blurred() -> np.ndarray:
    """Uniform grey — Laplacian variance exactly 0."""
    return np.full((96, 96, 3), 128, dtype=np.uint8)


@pytest.fixture()
def dataset(tmp_path: Path) -> Path:
    """5 train images: sharp / duplicate-of-sharp / blurred / corrupt /
    sharp2; 1 sharp val image. Labels: cls 0 for sharp, cls 1 for
    sharp2, empty (background) for the val image."""
    root = tmp_path / "ds_1"
    imgs = root / "images"
    labels = root / "labels"
    labels.mkdir(parents=True)

    _write_jpg(imgs / "1.jpg", _sharp(1))
    (imgs / "2.jpg").write_bytes((imgs / "1.jpg").read_bytes())   # duplicate
    _write_jpg(imgs / "3.jpg", _blurred())
    (imgs / "4.jpg").write_bytes(b"not a jpeg at all")            # corrupt
    _write_jpg(imgs / "5.jpg", _sharp(5))
    _write_jpg(imgs / "6.jpg", _sharp(6))                         # val

    (labels / "1.txt").write_text("0 0.5 0.5 0.4 0.4\n")
    (labels / "2.txt").write_text("0 0.5 0.5 0.4 0.4\n")
    (labels / "3.txt").write_text("0 0.5 0.5 0.4 0.4\n")
    (labels / "4.txt").write_text("1 0.5 0.5 0.4 0.4\n")
    (labels / "5.txt").write_text("1 0.5 0.5 0.4 0.4\n")
    (labels / "6.txt").write_text("")                             # background

    (root / "train.txt").write_text("\n".join(
        str(imgs / f"{i}.jpg") for i in (1, 2, 3, 4, 5)))
    (root / "val.txt").write_text(str(imgs / "6.jpg"))
    (root / "test.txt").write_text("")
    (root / "data.yaml").write_text(
        f"path: {root}\ntrain: train.txt\nval: val.txt\ntest: test.txt\n"
        f"nc: 2\nnames: ['uniform', 'no_uniform']\n")
    return root


def test_exclusions_and_manifest_rewrite(dataset: Path) -> None:
    report = sanitize_dataset(str(dataset))
    assert report["total_images"] == 6
    assert len(report["excluded_duplicate"]) == 1
    assert report["excluded_duplicate"][0].endswith("2.jpg")
    assert len(report["excluded_blurred"]) == 1
    assert report["excluded_blurred"][0].endswith("3.jpg")
    assert len(report["excluded_corrupt"]) == 1
    assert report["excluded_corrupt"][0].endswith("4.jpg")
    assert report["kept"] == 3
    assert report["kept_per_split"] == {"train": 2, "val": 1, "test": 0}
    # Manifests rewritten minus the excluded entries.
    train_now = (dataset / "train.txt").read_text().splitlines()
    assert [Path(p).name for p in train_now] == ["1.jpg", "5.jpg"]
    assert (dataset / "val.txt").read_text().splitlines() == [
        str(dataset / "images" / "6.jpg")]


def test_class_distribution_and_background(dataset: Path) -> None:
    report = sanitize_dataset(str(dataset))
    # Survivors: 1.jpg (cls uniform), 5.jpg (cls no_uniform), 6.jpg (bg).
    assert report["class_distribution"] == {"uniform": 1, "no_uniform": 1}
    assert report["background_images"] == 1
    # Both classes are below MIN_CLASS_INSTANCES → one warning each.
    assert len(report["imbalance_warnings"]) == 2


def test_report_only_mode_leaves_manifests(dataset: Path) -> None:
    before = (dataset / "train.txt").read_text()
    report = sanitize_dataset(str(dataset), apply=False)
    assert report["applied"] is False
    assert (dataset / "train.txt").read_text() == before


def test_blur_threshold_zero_keeps_flat_image(dataset: Path) -> None:
    report = sanitize_dataset(str(dataset), blur_threshold=0.0)
    assert report["excluded_blurred"] == []
    assert report["kept"] == 4          # only duplicate + corrupt excluded


def test_missing_root_raises() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        sanitize_dataset("/nonexistent/ds_999")


def test_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="train.txt"):
        sanitize_dataset(str(tmp_path))


def test_default_threshold_exported() -> None:
    assert DEFAULT_BLUR_THRESHOLD == 25.0
