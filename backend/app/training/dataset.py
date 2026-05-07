"""Dataset helpers — directory layout, image ingestion, train/val/test split."""
from __future__ import annotations
import logging
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


def write_yolo_dataset_yaml(db: Session, dataset_id: int) -> Path:
    """Write the YOLO data.yaml file the trainer reads.

    Layout:
      ds_<id>/
        data.yaml
        images/<file>
        labels/<file>.txt        ← YOLO-format labels written here
    """
    root = dataset_root(dataset_id)
    ds = db.get(Dataset, dataset_id)
    if not ds:
        raise ValueError("dataset not found")

    classes = list(ds.classes_json or [])
    cls_map = {c: i for i, c in enumerate(classes)}

    # Materialise YOLO label files for each labeled image, splitting paths.
    train_list, val_list, test_list = [], [], []
    images = (db.query(TrainingImage)
                .filter(TrainingImage.dataset_id == dataset_id,
                        TrainingImage.labeled == True)            # noqa: E712
                .all())
    for img in images:
        anns = db.query(Annotation).filter(Annotation.image_id == img.id).all()
        if not anns:
            continue
        rel = Path(img.file_path).resolve()
        label_path = root / "labels" / (Path(img.file_path).stem + ".txt")
        with label_path.open("w") as f:
            for a in anns:
                if a.class_label not in cls_map:
                    continue
                cx, cy, w, h = a.bbox_json
                f.write(f"{cls_map[a.class_label]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
        target_list = {"train": train_list, "val": val_list, "test": test_list}.get(img.split or "train", train_list)
        target_list.append(str(rel))

    def _write(name: str, items: list[str]) -> Path:
        p = root / f"{name}.txt"
        p.write_text("\n".join(items))
        return p

    train_txt = _write("train", train_list)
    val_txt   = _write("val", val_list or train_list)        # never leave val empty
    test_txt  = _write("test", test_list)

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
