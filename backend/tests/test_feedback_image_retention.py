from pathlib import Path

from app.config import settings
from app.training.feedback_loop import _persist_feedback_image


def test_feedback_image_is_copied_into_dataset_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path / "datasets"))
    source = tmp_path / "volatile-alert.jpg"
    source.write_bytes(b"alert evidence")

    saved = _persist_feedback_image(30, 103764, str(source))

    assert saved is not None
    saved_path = Path(saved)
    assert saved_path.read_bytes() == b"alert evidence"
    assert saved_path.parent == tmp_path / "datasets" / "ds_30" / "images"

    source.unlink()
    assert saved_path.read_bytes() == b"alert evidence"


def test_feedback_image_rejects_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path / "datasets"))

    assert _persist_feedback_image(
        30, 103764, str(tmp_path / "missing.jpg")
    ) is None
