"""Regression tests for training/validation isolation and pseudo labels."""
from __future__ import annotations

from types import SimpleNamespace

from app.models import Annotation, Dataset, TrainingImage
from app.training.pseudo_label import (_model_is_compatible,
                                        pseudo_label_image)


def test_pseudo_teacher_must_cover_every_dataset_class() -> None:
    model = SimpleNamespace(classes_json=["person", "vehicle"])

    assert _model_is_compatible(model, ["person"]) is True
    assert _model_is_compatible(model, ["person", "vehicle"]) is True
    assert _model_is_compatible(model, ["uniform_compliance"]) is False


def test_empty_dataset_class_map_accepts_teacher() -> None:
    model = SimpleNamespace(classes_json=["person"])

    assert _model_is_compatible(model, []) is True


def test_incompatible_pseudo_hit_is_not_persisted(
        tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"exists")
    image = SimpleNamespace(id=7, dataset_id=3, file_path=str(image_path),
                            labeled=False)
    dataset = SimpleNamespace(id=3, classes_json=["uniform_compliance"])

    class DB:
        added = []

        def get(self, model, row_id):
            if model is TrainingImage and row_id == 7:
                return image
            if model is Dataset and row_id == 3:
                return dataset
            return None

        def add(self, row):
            self.added.append(row)

        def commit(self):
            pass

    from app.training import annotation
    monkeypatch.setattr(annotation, "auto_suggest", lambda *args, **kwargs: [{
        "class_label": "person",
        "bbox_json": [0.5, 0.5, 0.2, 0.3],
    }])
    db = DB()

    assert pseudo_label_image(db, 7, weights="teacher.pt") == 0
    assert image.labeled is False
    assert db.added == []


def test_compatible_pseudo_hit_requires_human_verification(
        tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"exists")
    image = SimpleNamespace(id=8, dataset_id=4, file_path=str(image_path),
                            labeled=False)
    dataset = SimpleNamespace(id=4, classes_json=["person"])

    class DB:
        added = []

        def get(self, model, row_id):
            if model is TrainingImage and row_id == 8:
                return image
            if model is Dataset and row_id == 4:
                return dataset
            return None

        def add(self, row):
            self.added.append(row)

        def commit(self):
            pass

    from app.training import annotation
    monkeypatch.setattr(annotation, "auto_suggest", lambda *args, **kwargs: [{
        "class_label": "person",
        "bbox_json": [0.5, 0.5, 0.2, 0.3],
    }])
    db = DB()

    assert pseudo_label_image(db, 8, weights="teacher.pt") == 1
    assert image.labeled is True
    assert len(db.added) == 1
    assert isinstance(db.added[0], Annotation)
    assert db.added[0].auto_suggested is True
    assert db.added[0].verified is False


def test_alert_siblings_share_one_split() -> None:
    # Exercise split_dataset with a tiny query/session double.  Three frames
    # belong to one alert; the splitter must never scatter them across sets.
    from app.training.dataset import split_dataset

    images = [
        SimpleNamespace(id=1, source_alert_id=10, split=None),
        SimpleNamespace(id=2, source_alert_id=10, split=None),
        SimpleNamespace(id=3, source_alert_id=10, split=None),
        SimpleNamespace(id=4, source_alert_id=None, split=None),
        SimpleNamespace(id=5, source_alert_id=None, split=None),
    ]

    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return images

    class DB:
        def query(self, *args):
            return Query()

        def commit(self):
            pass

    counts = split_dataset(DB(), 1, train=0.6, val=0.4, test=0.0)

    assert len({img.split for img in images[:3]}) == 1
    assert sum(counts.values()) == len(images)


def test_same_camera_day_never_leaks_across_splits() -> None:
    from app.training.dataset import split_dataset

    images = [
        SimpleNamespace(id=i, source_alert_id=i, split=None, camera_id=7,
                        captured_at=None,
                        source_extra={"timestamp_iso": "2026-09-03T08:00:00+00:00"})
        for i in range(1, 5)
    ]

    class Query:
        def filter(self, *args): return self
        def all(self): return images

    class DB:
        def query(self, *args): return Query()
        def commit(self): pass

    split_dataset(DB(), 1, train=0.5, val=0.5, test=0.0)
    assert len({img.split for img in images}) == 1
