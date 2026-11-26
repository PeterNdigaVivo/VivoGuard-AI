from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.training.dataset import save_uploaded_image, settings
from app.training.image_preview import build_preview_path


def test_repeated_miner_names_stay_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    payloads = [str(number).encode() for number in range(120)]

    paths = [save_uploaded_image(1, "mined_cam1_uniform.jpg", data) for data in payloads]

    assert paths[0].name == "mined_cam1_uniform.jpg"
    assert len(set(paths)) == len(payloads)
    assert [path.read_bytes() for path in paths] == payloads
    assert all(len(path.name.encode("utf-8")) <= 255 for path in paths)


@pytest.mark.parametrize("filename", ["界" * 110 + ".png", r"..\camera\sample.png", "CON.png"])
def test_upload_name_preserves_extension_and_preview_room(monkeypatch, tmp_path, filename):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))

    first = save_uploaded_image(2, filename, b"first")
    second = save_uploaded_image(2, filename, b"second")

    assert first != second
    for path, data in ((first, b"first"), (second, b"second")):
        assert path.parent == tmp_path / "ds_2" / "images"
        assert path.suffix == ".png"
        assert path.read_bytes() == data
        assert len(Path(build_preview_path(str(path))).name.encode("utf-8")) <= 255


def test_concurrent_uploads_keep_each_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "datasets_dir", str(tmp_path))
    payloads = [str(number).encode() for number in range(24)]

    with ThreadPoolExecutor(max_workers=8) as workers:
        paths = list(workers.map(lambda data: save_uploaded_image(3, "crop.jpg", data), payloads))

    assert len(set(paths)) == len(payloads)
    assert [path.read_bytes() for path in paths] == payloads
