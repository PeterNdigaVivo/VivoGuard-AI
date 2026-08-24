from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from app.ai.env_config import (
    _configure_onnxruntime_sessions,
    configure_cpu_thread_budget,
)


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


def test_onnxruntime_sessions_receive_bounded_options():
    original = Mock(return_value="session")
    original._vivoguard_thread_bounded = False
    options = Mock()
    ort = ModuleType("onnxruntime")
    ort.InferenceSession = original
    ort.SessionOptions = Mock(return_value=options)
    ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")

    with patch.dict("sys.modules", {"onnxruntime": ort}):
        assert _configure_onnxruntime_sessions(1)
        assert ort.InferenceSession("model.onnx", providers=["CPU"]) == "session"

    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"
    options.add_session_config_entry.assert_any_call(
        "session.intra_op.allow_spinning", "0"
    )
    options.add_session_config_entry.assert_any_call(
        "session.inter_op.allow_spinning", "0"
    )
    original.assert_called_once_with(
        "model.onnx", providers=["CPU"], sess_options=options
    )


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
