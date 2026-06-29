"""inference_perf_log — per-camera inference latency telemetry

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-25

One row per flushed window (every 100 frames) from PerfTracker,
holding the TOTAL-stage latency percentiles. The per-stage
breakdown (frame_read / inference / postprocess / emit) lives in
Redis (vg:perf:{camera_id}) for the live System panel and is NOT
columnised here — the table is the historical p50/p95/p99/avg
record powering GET /analytics/perf.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inference_perf_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("camera_id", sa.Integer,
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("timestamp", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False, index=True),
        sa.Column("frame_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("p50_ms", sa.Float, nullable=True),
        sa.Column("p95_ms", sa.Float, nullable=True),
        sa.Column("p99_ms", sa.Float, nullable=True),
        sa.Column("avg_ms", sa.Float, nullable=True),
        sa.Column("backend", sa.String(16), nullable=True),
        sa.Column("format",  sa.String(16), nullable=True),
    )
    op.create_index("ix_inference_perf__cam_ts",
                    "inference_perf_log", ["camera_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_inference_perf__cam_ts", table_name="inference_perf_log")
    op.drop_table("inference_perf_log")
