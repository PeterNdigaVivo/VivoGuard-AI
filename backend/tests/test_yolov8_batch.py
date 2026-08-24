from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from app.ai import yolov8_runner


class _Tensor:
    def __init__(self, values):
        self.values = np.asarray(values)

    def cpu(self):
        return self

    def numpy(self):
        return self.values


class _Boxes(SimpleNamespace):
    def __len__(self):
        return len(self.conf.values)


def _result(box, confidence, cls_id):
    boxes = _Boxes(
        xyxy=_Tensor([box]), conf=_Tensor([confidence]), cls=_Tensor([cls_id]),
    )
    return SimpleNamespace(boxes=boxes, names={0: "person"})


def test_infer_batch_preserves_input_order_and_normalises(monkeypatch):
    frames = [
        np.zeros((100, 200, 3), dtype=np.uint8),
        np.zeros((200, 400, 3), dtype=np.uint8),
    ]
    model = Mock()
    model.predict.return_value = [
        _result([20, 10, 100, 50], 0.9, 0),
        _result([40, 20, 200, 100], 0.8, 0),
    ]
    monkeypatch.setattr(yolov8_runner, "load_model", lambda _weights: model)
    monkeypatch.setattr(yolov8_runner, "resolve_weights", lambda _weights: "model")
    monkeypatch.setattr(
        yolov8_runner, "_hardware", lambda: SimpleNamespace(device="cuda:0"),
    )

    output = yolov8_runner.infer_batch(frames)

    assert [row[0]["conf"] for row in output] == [0.9, 0.8]
    assert output[0][0]["bbox_norm"] == [0.1, 0.1, 0.5, 0.5]
    assert output[1][0]["bbox_norm"] == [0.1, 0.1, 0.5, 0.5]
    model.predict.assert_called_once()
    assert model.predict.call_args.args[0] is frames


def test_infer_batch_empty_input_does_not_load_model(monkeypatch):
    load = Mock()
    monkeypatch.setattr(yolov8_runner, "load_model", load)
    assert yolov8_runner.infer_batch([]) == []
    load.assert_not_called()


def test_single_frame_infer_preserves_established_non_batch_hot_path(monkeypatch):
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    model = Mock()
    model.predict.return_value = [_result([20, 10, 100, 50], 0.9, 0)]
    monkeypatch.setattr(yolov8_runner, "load_model", lambda _weights: model)
    monkeypatch.setattr(yolov8_runner, "resolve_weights", lambda _weights: "model")
    monkeypatch.setattr(
        yolov8_runner, "_hardware", lambda: SimpleNamespace(device="cpu"),
    )

    assert yolov8_runner.infer(frame)[0]["bbox_norm"] == [0.1, 0.1, 0.5, 0.5]
    assert model.predict.call_args.args[0] is frame
