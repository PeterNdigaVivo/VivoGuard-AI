"""Pre-deployment model validation (automated model gating).

Every AUTOMATED path that flips `AIModel.deployed` runs the candidate
through `validate_model_before_deploy` first:

  1. class-map check     — the model's embedded names must exactly match
                           the classes the platform expects it to serve
                           (a specialist model silently replacing a COCO
                           model is how the July persons=0 outage began);
  2. inference smoke test — one dummy-tensor predict() proves the weights
                           load and execute on this host (no CUDA/CPU or
                           serialization surprises at 3am).

Manual deploys (`/training/models/{id}/deploy`, `promote(force=True)`)
BYPASS the gate on purpose: that endpoint is the operator's rollback and
override mechanism, and a gate there could block an emergency rollback.

Verdicts are returned (True/False) and logged at ERROR on rejection —
callers decide whether to surface a dict verdict or raise ModelGateError.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DUMMY_IMAGE_SIZE: int = 640


def _model_class_names(model: object) -> list[str]:
    """Normalise ultralytics `model.names` (dict[int,str] | list[str])
    into an index-ordered list."""
    names = getattr(model, "names", None)
    if isinstance(names, dict):
        return [str(names[k]) for k in sorted(names)]
    if isinstance(names, (list, tuple)):
        return [str(n) for n in names]
    return []


def validate_model_before_deploy(model_path: str,
                                 required_classes: list[str],
                                 *, imgsz: int = DUMMY_IMAGE_SIZE) -> bool:
    """True when the model at `model_path` is safe to deploy.

    Checks, in order:
      - weights file loads via ultralytics YOLO;
      - class map EXACTLY matches `required_classes` (index order
        included). An empty `required_classes` skips the class check
        (nothing to validate against) but still smoke-tests inference;
      - a dummy inference pass executes without error.

    Never raises — every failure is logged at ERROR with the reason and
    returns False so the caller blocks the deploy.
    """
    try:
        import numpy as np
        from ultralytics import YOLO
    except ImportError as e:                              # pragma: no cover
        log.error("model gate REJECT %s: ultralytics/numpy unavailable: %s",
                  model_path, e)
        return False

    try:
        model = YOLO(model_path)
    except Exception as e:
        log.error("model gate REJECT %s: weights failed to load: %s",
                  model_path, e, exc_info=True)
        return False

    got = _model_class_names(model)
    required = [str(c) for c in (required_classes or [])]
    if required and got != required:
        log.error("model gate REJECT %s: class map mismatch — model=%s "
                  "required=%s", model_path, got, required)
        return False

    try:
        dummy = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
        model.predict(dummy, verbose=False)
    except Exception as e:
        log.error("model gate REJECT %s: dummy inference failed: %s",
                  model_path, e, exc_info=True)
        return False

    log.info("model gate PASS %s (classes=%s)", model_path, got or "(none)")
    return True


def check_metric_regression(candidate_map50: float | None,
                            baseline_map50: float | None,
                            *, max_regression: float = 0.0
                            ) -> tuple[bool, str]:
    """(ok, reason) — candidate must meet-or-exceed the baseline map50,
    tolerating at most `max_regression` absolute drop (default 0.0 =
    strict no-regression, the platform's historical behaviour;
    MODEL_GATE_MAX_REGRESSION relaxes it, e.g. 0.05 per ML best
    practice). Missing metrics pass — volume-based gates handle those."""
    if candidate_map50 is None or baseline_map50 is None:
        return True, "metrics unavailable — regression check skipped"
    if candidate_map50 < baseline_map50 - max_regression:
        return False, (f"candidate map50 {candidate_map50:.4f} < baseline "
                       f"{baseline_map50:.4f} - {max_regression:.2f} tolerance")
    return True, (f"candidate map50 {candidate_map50:.4f} within tolerance "
                  f"of baseline {baseline_map50:.4f}")
