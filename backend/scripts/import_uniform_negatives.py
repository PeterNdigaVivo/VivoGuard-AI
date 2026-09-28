"""Import mined `civilian` crops into training_samples as the negative class.

The uniform pipeline collects its two halves into two different tables.
staff_identity writes POSITIVES (`vivo_all_black`) to TrainingSample;
uniform_miner writes NEGATIVES (`civilian`) to a Dataset/TrainingImage
pool. chain_training reads only TrainingSample, so it has never seen a
single negative example — and a classifier trained on nothing but staff
answers "staff" for everyone, which is exactly the failure that lets an
unidentified person stand at a till unreported.

This copies the existing negatives across so the first training run has
a real negative class. It is a migration, not a pipeline: the lasting
fix is for the miner to write both halves to the same table. Until that
lands, re-running this picks up anything new.

Idempotent — a file already present in training_samples is skipped, so
running it twice cannot duplicate the dataset.

    docker compose exec -T worker-training \
        python scripts/import_uniform_negatives.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

DATASET = "feedback-negative-uniform"
LABEL = "civilian"
# chain_training only accepts rows whose source is in its allow-list;
# 'upload' is the honest description of an operator-curated pool.
SOURCE = "upload"


def main(dry_run: bool = False) -> int:
    from app.database import SessionLocal
    from app.models import Dataset, TrainingImage, TrainingSample

    with SessionLocal() as db:
        ds = db.query(Dataset).filter(Dataset.name == DATASET).first()
        if ds is None:
            print(f"dataset {DATASET!r} not found — nothing to import")
            return 1

        images = (db.query(TrainingImage)
                    .filter(TrainingImage.dataset_id == ds.id)
                    .all())
        # One round-trip for the dedup set rather than a query per image.
        have = {p for (p,) in db.query(TrainingSample.frame_path)
                                .filter(TrainingSample.detector_type == "uniform")
                                .all()}

        added = skipped_dup = skipped_missing = 0
        for img in images:
            if img.file_path in have:
                skipped_dup += 1
                continue
            # A row pointing at a file the trainer cannot open is worse
            # than no row: it survives every filter until Path.exists().
            if not Path(img.file_path).exists():
                skipped_missing += 1
                continue
            if not dry_run:
                db.add(TrainingSample(
                    detector_type="uniform",
                    label=LABEL,
                    camera_id=img.camera_id,
                    store_id=(img.source_extra or {}).get("store_id"),
                    frame_path=img.file_path,
                    preview_path=img.preview_path,
                    captured_at=img.captured_at,
                    source=SOURCE,
                    # Mined by rule (not black AND outside every staff
                    # zone), so it carries the same confidence as the
                    # auto-harvested positives, which are also pending.
                    approved=None,
                    shared=True,
                ))
            have.add(img.file_path)
            added += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()

        print(f"dataset {DATASET}: {len(images)} images")
        print(f"  added         {added}{' (dry run — rolled back)' if dry_run else ''}")
        print(f"  already there {skipped_dup}")
        print(f"  file missing  {skipped_missing}")
        return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
