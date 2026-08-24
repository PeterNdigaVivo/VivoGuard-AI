from pathlib import Path
from unittest.mock import Mock, patch

from app.ai.env_config import configure_cpu_thread_budget


def test_cpu_thread_budget_configures_loaded_runtimes(monkeypatch):
    monkeypatch.setenv("INFERENCE_LIBRARY_THREADS", "2")
    cv2 = Mock()
    torch = Mock()

    with patch.dict("sys.modules", {"cv2": cv2, "torch": torch}):
        assert configure_cpu_thread_budget("cpu") == 2

    cv2.setNumThreads.assert_called_once_with(2)
    torch.set_num_threads.assert_called_once_with(2)
    torch.set_num_interop_threads.assert_called_once_with(2)


def test_gpu_thread_budget_keeps_framework_defaults():
    assert configure_cpu_thread_budget("cuda") == 0


def test_compose_caps_every_native_inference_pool():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text()

    worker = compose.split("  worker-inference:", 1)[1].split(
        "  worker-alerts:", 1
    )[0]
    assert "INFERENCE_LIBRARY_THREADS:-1" in worker
    for variable in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "OPENCV_FOR_THREADS_NUM",
    ):
        assert variable in worker
