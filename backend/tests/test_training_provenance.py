from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Dataset, TrainingImage
from app.training.dataset import split_dataset


def test_quarantined_simulation_image_is_excluded_from_split(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        dataset = Dataset(name="provenance-test", classes_json=["person"])
        db.add(dataset)
        db.flush()
        approved = TrainingImage(dataset_id=dataset.id, file_path=str(tmp_path / "approved.jpg"),
                                 labeled=True, source_kind="operator_verified",
                                 eligible_for_training=True, review_state="approved")
        quarantined = TrainingImage(dataset_id=dataset.id, file_path=str(tmp_path / "synthetic.jpg"),
                                    labeled=True, source_kind="synthetic",
                                    eligible_for_training=False, review_state="pending",
                                    simulation_run_id="sim-1")
        db.add_all([approved, quarantined])
        db.commit()
        counts = split_dataset(db, dataset.id, train=1, val=0, test=0)
        db.refresh(approved)
        db.refresh(quarantined)
        assert counts == {"train": 1, "val": 0, "test": 0}
        assert approved.split == "train"
        assert quarantined.split is None
