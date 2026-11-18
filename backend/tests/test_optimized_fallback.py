"""Optimized (TensorRT) model loading must never take detection down.

A fake `ultralytics` stands in for the real one (CI has no model stack).
Its "engine" can be told to fail on first predict(), which is how an
engine built by another TensorRT version fails in production.
"""
import json
import logging
import sys
import types
from pathlib import Path

import pytest

from app.ai import env_config
from app.ai.env_config import HardwareEnv


class _FakeYOLO:
    engine_fails = False
    loaded: list[str] = []
    exports = 0

    def __init__(self, path):
        self.path = str(path)
        _FakeYOLO.loaded.append(self.path)

    def predict(self, frame, **kwargs):
        if self.path.endswith(".engine") and _FakeYOLO.engine_fails:
            # What Ultralytics raises when TensorRT returns no engine.
            raise AttributeError("'NoneType' object has no attribute "
                                 "'create_execution_context'")
        return []

    def export(self, format, **kwargs):
        # Like Ultralytics: write the artifact next to the source weights.
        _FakeYOLO.exports += 1
        out = Path(self.path).with_suffix(".engine")
        out.write_bytes(b"engine")
        return str(out)


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    module = types.ModuleType("ultralytics")
    module.YOLO = _FakeYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    _FakeYOLO.engine_fails = False
    _FakeYOLO.loaded = []
    _FakeYOLO.exports = 0

    from app.config import settings
    monkeypatch.setattr(settings, "models_dir", str(tmp_path / "models"))
    monkeypatch.setattr(settings, "inference_max_batch_size", 1)
    versions = {"ultralytics": "8.4.1", "tensorrt": "10.13.0"}
    monkeypatch.setattr(env_config, "_package_version", lambda n: versions.get(n, "none"))
    monkeypatch.setattr(env_config, "_cuda_arch", lambda: "sm120")

    weights = tmp_path / "yolov8n.pt"
    weights.write_bytes(b"weights")
    env = HardwareEnv(backend="cuda", device="cuda:0", export_format="engine")
    return env, weights, tmp_path / "models" / "optimized", versions


def test_working_engine_is_built_once_in_private_dir_and_reused(fake_env):
    env, weights, optimized, _ = fake_env

    model, fmt = env.load_optimized(str(weights))
    assert fmt == "engine"
    assert model.path.endswith(".engine")
    # Exported in a private temp dir, never next to the shared weights.
    assert not weights.with_suffix(".engine").exists()
    assert list(optimized.glob("*/yolov8n.engine"))

    _, fmt_again = env.load_optimized(str(weights))
    assert fmt_again == "engine"
    assert _FakeYOLO.exports == 1


def test_engine_that_fails_on_first_predict_falls_back_to_pytorch(fake_env, caplog):
    env, weights, optimized, _ = fake_env
    _FakeYOLO.engine_fails = True

    with caplog.at_level(logging.WARNING, logger="app.ai.env_config"):
        model, fmt = env.load_optimized(str(weights))

    assert fmt == "pytorch"
    assert model.path == str(weights)
    assert "falling back to raw PyTorch" in caplog.text
    assert "create_execution_context" in caplog.text
    marker = next(optimized.glob("*/LOAD_FAILED.json"))
    assert "create_execution_context" in json.loads(marker.read_text())["error"]


def test_known_bad_engine_is_not_retried_on_restart(fake_env, caplog):
    env, weights, _, _ = fake_env
    _FakeYOLO.engine_fails = True
    env.load_optimized(str(weights))
    _FakeYOLO.loaded = []

    with caplog.at_level(logging.WARNING, logger="app.ai.env_config"):
        model, fmt = env.load_optimized(str(weights))

    assert fmt == "pytorch"
    assert _FakeYOLO.loaded == [str(weights)]      # engine not even opened
    assert "Delete that directory to retry" in caplog.text


def test_new_tensorrt_version_gets_its_own_engine(fake_env):
    env, weights, optimized, versions = fake_env
    _FakeYOLO.engine_fails = True
    env.load_optimized(str(weights))               # old engine marked bad

    versions["tensorrt"] = "10.14.0"               # image rebuilt
    _FakeYOLO.engine_fails = False
    _, fmt = env.load_optimized(str(weights))

    assert fmt == "engine"
    assert len(list(optimized.iterdir())) == 2


def test_cache_key_names_everything_an_engine_depends_on(fake_env):
    env, _, _, versions = fake_env
    key = env._cache_key("yolov8n", "engine", 1, 640)
    assert key == "yolov8n_cuda_i640_ul8.4.1_ten10.13.0_sm120"
    assert env._cache_key("yolov8n", "engine", 8, 640) != key
    assert env._cache_key("yolov8n", "engine", 1, 960) != key
    versions["tensorrt"] = "10.14.0"
    assert env._cache_key("yolov8n", "engine", 1, 640) != key


def test_optimization_off_loads_pytorch_without_exporting(fake_env):
    env, weights, _, _ = fake_env
    _, fmt = env.load_optimized(str(weights), use_optimized=False)
    assert fmt == "pytorch"
    assert _FakeYOLO.exports == 0


def test_eval_labels_and_warns_about_pytorch_fallback(monkeypatch, caplog):
    from app.ai import yolov8_runner
    from app.eval import run

    model = types.SimpleNamespace(_vg_format="pytorch")
    monkeypatch.setattr(yolov8_runner, "load_model", lambda _w, _imgsz=640: model)
    monkeypatch.setattr(yolov8_runner, "resolve_weights", lambda n: n)

    with caplog.at_level(logging.WARNING, logger="app.eval.run"):
        infer_fn = run._detector_infer_fn("yolov8n.pt", 0.25, 640)

    assert infer_fn.model_format == "pytorch"
    assert "PyTorch speed, not TensorRT" in caplog.text
