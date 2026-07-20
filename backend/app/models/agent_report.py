"""AgentReport — one structured row per autonomous-monitoring-agent run.

Written by every agent in app/tasks/agents.py via `_write_report`. The
Inspection agent reads the last 24h to build its daily digest and prunes
rows older than 30 days.
"""
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _json():
    """JSONB on Postgres, portable JSON on the SQLite edge build."""
    return JSON().with_variant(JSONB, "postgresql")


class AgentReport(Base):
    __tablename__ = "agent_reports"

    id:            Mapped[int]      = mapped_column(primary_key=True)
    # No single-column index=True here — migration 0030 creates only the
    # composite (agent_name, run_at) below, which covers the agent-name +
    # latest-run queries. Declaring index=True too made create_all (sqlite
    # edge / tests) build 3 indexes vs the migration's 1 (autogenerate drift).
    agent_name:    Mapped[str]      = mapped_column(String(64))
    run_at:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    # ok | warning | critical
    status:        Mapped[str]      = mapped_column(String(16))
    findings:      Mapped[dict | None] = mapped_column(_json(), nullable=True)
    actions_taken: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    gaps:          Mapped[dict | None] = mapped_column(_json(), nullable=True)
    duration_ms:   Mapped[int | None]  = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None]  = mapped_column(Text, nullable=True)


# Matches the migration index — the agents/reports API and the watchdog
# both query "latest report per agent".
Index("ix_agent_reports_name_run_at", AgentReport.agent_name, AgentReport.run_at)
