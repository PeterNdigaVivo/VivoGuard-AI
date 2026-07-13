"""Dataset helpers — directory layout, image ingestion, train/val/test split."""
from __future__ import annotations
import logging
import os
import random
import shutil
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Dataset, TrainingImage, Annotation

log = logging.getLogger(__name__)


def dataset_root(dataset_id: int) -> Path:
    """Filesystem root for a dataset's images + labels."""
    p = Path(settings.datasets_dir) / f"ds_{dataset_id}"
    (p / "images").mkdir(parents=True, exist_ok=True)
    (p / "labels").mkdir(parents=True, exist_ok=True)
    return p


def save_uploaded_image(dataset_id: int, filename: str, data: bytes) -> Path:
    """Persist a single image. Returns the on-disk path."""
    root = dataset_root(dataset_id)
    safe = filename.replace("/", "_").replace("..", "")
    target = root / "images" / safe
    n = 1
    while target.exists():
        stem, suf = target.stem, target.suffix
        target = target.with_name(f"{stem}_{n}{suf}")
        n += 1
    target.write_bytes(data)
    return target


def ingest_zip(dataset_id: int, zip_bytes: bytes) -> int:
    """Extract image files from a zip into the dataset. Returns count added."""
    root = dataset_root(dataset_id)
    added = 0
    with zipfile.ZipFile_BytesIO_compat(zip_bytes) as zf:                # type: ignore
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if not name.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                continue
            data = zf.read(name)
            save_uploaded_image(dataset_id, Path(name).name, data)
            added += 1
    return added


# stdlib zipfile doesn't take bytes; small helper to keep callers simple.
def _zipfile_from_bytes(b: bytes) -> zipfile.ZipFile:
    import io
    return zipfile.ZipFile(io.BytesIO(b))
zipfile.ZipFile_BytesIO_compat = _zipfile_from_bytes                     # type: ignore


def split_dataset(db: Session, dataset_id: int, *,
                  train: float = 0.7, val: float = 0.2, test: float = 0.1,
                  seed: int = 1337) -> dict[str, int]:
    """Stamp each image's `split` column. Idempotent — re-running re-splits."""
    images = (db.query(TrainingImage)
                .filter(TrainingImage.dataset_id == dataset_id,
                        TrainingImage.labeled == True)            # noqa: E712
                .all())
    random.Random(seed).shuffle(images)
    n = len(images)
    n_train = int(n * train)
    n_val   = int(n * val)
    counts = {"train": 0, "val": 0, "test": 0}
    for i, img in enumerate(images):
        if i < n_train:
            img.split = "train"
        elif i < n_train + n_val:
            img.split = "val"
        else:
            img.split = "test"
        counts[img.split] += 1
    db.commit()
    return counts


def write_yolo_dataset_yaml(db: Session, dataset_id: int, *,
                            extra_dataset_ids: list[int] | None = None,
                            max_neg_ratio: float = 3.0) -> Path:
    """Write the YOLO data.yaml file the trainer reads.

    Layout:
      ds_<id>/
        data.yaml
        images/<file>
        labels/<file>.txt        ← YOLO-format labels written here

    Background images (TrainingImage rows with no Annotation rows) are
    now included as hard negatives — YOLO treats an image whose label
    .txt file is EMPTY as "this frame contains no objects of interest,
    suppress false detections here." Background images are capped at
    `max_neg_ratio × positive_count` per split so a flood of dismissed
    alerts can't swamp the gradient with negatives (YOLO best
    practice: ≤ 30% background).

    `extra_dataset_ids` lets a fine-tune job pull negatives from a
    SECOND dataset (typically `feedback-negative-<type>`) without
    moving rows across the TrainingImage FK — useful for the
    Phase 2 feedback fine-tune pipeline.
    """
    root = dataset_root(dataset_id)
    ds = db.get(Dataset, dataset_id)
    if not ds:
        raise ValueError("dataset not found")

    classes = list(ds.classes_json or [])
    # Negative-only datasets ship with an empty classes_json — fall
    # back to the positive dataset's classes so cls_map is non-empty.
    cls_map = {c: i for i, c in enumerate(classes)}

    # Pull images from the primary dataset + any extras (typically the
    # paired hard-negative pool). Dedup is unnecessary because each
    # TrainingImage row has a single dataset_id FK.
    dataset_ids = [dataset_id, *(extra_dataset_ids or [])]
    images = (db.query(TrainingImage)
                .filter(TrainingImage.dataset_id.in_(dataset_ids),
                        TrainingImage.labeled == True)            # noqa: E712
                .all())

    # Pass 1 — bucket per split into (positives, negatives) so we can
    # apply the per-split negative cap fairly.
    by_split: dict[str, dict[str, list]] = {}
    for img in images:
        anns = db.query(Annotation).filter(Annotation.image_id == img.id).all()
        split = img.split or "train"
        bucket = by_split.setdefault(split, {"pos": [], "neg": []})
        if anns:
            bucket["pos"].append((img, anns))
        else:
            bucket["neg"].append(img)

    train_list, val_list, test_list = [], [], []
    out_lists = {"train": train_list, "val": val_list, "test": test_list}

    def _stage_image(img) -> str | None:
        """Copy the source image into <root>/images/<img.id><ext> so YOLO's
        images/ -> labels/ path-swap resolves to the label we write. We COPY
        (never symlink) — symlinks don't survive crossing Docker volume
        boundaries. Filenames are keyed on TrainingImage.id (globally unique),
        so two snapshots sharing a basename can no longer overwrite each
        other's label. Returns the staged path, or None if the source file is
        missing/unreadable."""
        src = Path(img.file_path)
        if not src.exists():
            log.warning("dataset %s: training image %s missing on disk (%s) — skipped",
                        dataset_id, img.id, src)
            return None
        ext = src.suffix.lower() or ".jpg"
        staged = root / "images" / f"{img.id}{ext}"
        try:
            shutil.copy2(src, staged)
        except Exception as e:
            log.warning("dataset %s: failed to stage image %s (%s): %s",
                        dataset_id, img.id, src, e)
            return None
        return str(staged)

    for split, bucket in by_split.items():
        target = out_lists.get(split, train_list)
        # Positives: stage the image, then write its YOLO label to
        # <root>/labels/<img.id>.txt so YOLO's images/->labels/ swap on the
        # STAGED path finds it. (Listing the original file_path — an alert
        # snapshot outside <root>/images/ — is what silently zeroed mAP.)
        for img, anns in bucket["pos"]:
            staged = _stage_image(img)
            if staged is None:
                continue
            label_path = root / "labels" / f"{img.id}.txt"
            with label_path.open("w") as f:
                for a in anns:
                    if a.class_label not in cls_map:
                        continue
                    cx, cy, w, h = a.bbox_json
                    f.write(f"{cls_map[a.class_label]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
            target.append(staged)

        # Negatives: capped at max_neg_ratio × positives. An EMPTY label
        # file is YOLO's "background" convention.
        pos_count = len(bucket["pos"])
        neg_cap = int(pos_count * float(max_neg_ratio)) if pos_count else 0
        # Edge case — a feedback-only fine-tune with zero positives:
        # still allow a few negatives so the trainer has SOMETHING.
        if pos_count == 0 and bucket["neg"]:
            neg_cap = int(max_neg_ratio) or 1
        for img in bucket["neg"][:neg_cap]:
            staged = _stage_image(img)
            if staged is None:
                continue
            (root / "labels" / f"{img.id}.txt").write_text("")   # background
            target.append(staged)

    def _write(name: str, items: list[str]) -> Path:
        p = root / f"{name}.txt"
        p.write_text("\n".join(items))
        return p

    # SECONDARY FIX — never validate on the training set. The old
    # `val_list or train_list` fallback silently made val == train, which
    # corrupts mAP. Require a real held-out val set; refuse to train
    # otherwise (belt-and-braces behind the orchestrator's 30-image floor).
    MIN_VAL_IMAGES = 5
    if len(val_list) < MIN_VAL_IMAGES:
        raise ValueError(
            f"insufficient validation images: {len(val_list)} < {MIN_VAL_IMAGES} "
            f"(train={len(train_list)}, test={len(test_list)}). Refusing to "
            f"train: a val set this small yields meaningless mAP and the "
            f"previous train-set fallback corrupted metrics. Add more labelled "
            f"images, or lower split_val so more land in the val split."
        )

    train_txt = _write("train", train_list)
    val_txt   = _write("val", val_list)      # NO train fallback (secondary fix)
    test_txt  = _write("test", test_list)

    # Diagnostic — confirm images are staged and their labels resolve via
    # YOLO's images/ -> labels/ swap (the bug that zeroed every mAP).
    def _yolo_label_path(img_path: str) -> Path:
        sa, sb = f"{os.sep}images{os.sep}", f"{os.sep}labels{os.sep}"
        return Path(sb.join(img_path.rsplit(sa, 1))).with_suffix(".txt")

    sample = (train_list[:1] or ["-"])[0]
    label_ok = _yolo_label_path(sample).exists() if sample != "-" else False
    log.info("write_yolo_dataset_yaml ds=%s: train.txt=%d val.txt=%d test.txt=%d "
             "images; sample img=%s label_exists=%s",
             dataset_id, len(train_list), len(val_list), len(test_list),
             sample, label_ok)

    yaml_path = root / "data.yaml"
    yaml_path.write_text(
        f"path: {root}\n"
        f"train: {train_txt.relative_to(root)}\n"
        f"val: {val_txt.relative_to(root)}\n"
        f"test: {test_txt.relative_to(root)}\n"
        f"nc: {len(classes)}\n"
        f"names: {classes}\n"
    )
    return yaml_path
