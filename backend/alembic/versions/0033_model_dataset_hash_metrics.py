"""ai_models.dataset_hash + ai_models.validation_metrics_json

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-31

Experiment-tracking columns (ML-pipeline hardening):
  - ai_models.dataset_hash — SHA256 fingerprint of the staged dataset the
    model trained on (see app.training.training_logger.compute_dataset_hash);
    basic dataset versioning / reproducibility.
  - ai_models.validation_metrics_json — the full final-validation metric
    dict (map50, map50_95, precision, recall, …) as one JSON blob so the
    complete picture survives even as individual metric columns evolve.

Both nullable — existing rows keep working untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def upgrade() -> None:
    am_cols = _columns(op.get_bind(), "ai_models")
    if "dataset_hash" not in am_cols:
        op.add_column("ai_models",
                      sa.Column("dataset_hash", sa.String(64), nullable=True))
    if "validation_metrics_json" not in am_cols:
        op.add_column("ai_models",
                      sa.Column("validation_metrics_json", sa.JSON(),
                                nullable=True))


def downgrade() -> None:
    am_cols = _columns(op.get_bind(), "ai_models")
    if "validation_metrics_json" in am_cols:
        op.drop_column("ai_models", "validation_metrics_json")
    if "dataset_hash" in am_cols:
        op.drop_column("ai_models", "dataset_hash")
