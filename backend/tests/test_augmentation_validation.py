"""Tests for trainer.validate_augmentation_config — the fail-fast gate
that replaced the silent TypeError-retry.

`valid_keys` is injected so the test runs without ultralytics installed;
the production path derives the set from DEFAULT_CFG_DICT.
"""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")            # trainer's module-level deps
pytest.importorskip("redis")
pytest.importorskip("pydantic_settings")

from app.training.trainer import (           # noqa: E402
    DEFAULT_AUGMENTATION, validate_augmentation_config,
)

VALID = {"degrees", "translate", "scale", "fliplr", "mosaic",
         "hsv_h", "hsv_s", "hsv_v", "mixup"}


def test_defaults_pass() -> None:
    validate_augmentation_config(dict(DEFAULT_AUGMENTATION), valid_keys=VALID)


def test_unknown_key_raises_with_name() -> None:
    aug = {**DEFAULT_AUGMENTATION, "not_a_real_arg": 1.0}
    with pytest.raises(ValueError, match="not_a_real_arg"):
        validate_augmentation_config(aug, valid_keys=VALID)


def test_non_numeric_value_raises_with_name() -> None:
    aug = {**DEFAULT_AUGMENTATION, "mosaic": "half"}
    with pytest.raises(ValueError, match="mosaic"):
        validate_augmentation_config(aug, valid_keys=VALID)


def test_bool_value_rejected() -> None:
    # bool is an int subclass — explicitly rejected (True is not a ratio).
    aug = {**DEFAULT_AUGMENTATION, "fliplr": True}
    with pytest.raises(ValueError, match="fliplr"):
        validate_augmentation_config(aug, valid_keys=VALID)


def test_empty_config_passes() -> None:
    validate_augmentation_config({}, valid_keys=VALID)
