"""Typed exceptions for the training pipeline.

Every gate in the pipeline raises one of these instead of failing
silently — the Celery task layer marks the job failed with the message,
so the reason is visible in the Training Studio UI and worker logs.
"""
from __future__ import annotations


class TrainingPipelineError(RuntimeError):
    """Base class for training-pipeline failures."""


class InsufficientDataError(TrainingPipelineError):
    """Combined dataset (positives + negatives + base-mix) is below the
    minimum viable size (settings.min_training_images, default 50).
    Training on fewer images yields noise metrics and overfit weights."""


class ModelGateError(TrainingPipelineError):
    """A trained model failed pre-deployment validation (class-map
    mismatch, inference smoke-test failure, or metric regression versus
    its parent) and must not be deployed."""
