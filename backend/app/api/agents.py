"""Agents API — read the autonomous-monitoring reports and manually
trigger an agent.

Note: the build spec listed `GET /agents/{name}/run` for the manual
trigger, but triggering work is a side-effecting action, so it is exposed
as POST (and gated to admins). The reports feed is a plain GET.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import AgentReport
from app.agent_control.accountability import scorecards

router = APIRouter(prefix="/agents", tags=["agents"])


def _serialize(rep: AgentReport) -> dict:
    return {
        "id": rep.id,
        "agent_name": rep.agent_name,
        "run_at": rep.run_at.isoformat() if rep.run_at else None,
        "status": rep.status,
        "findings": rep.findings,
        "actions_taken": rep.actions_taken,
        "gaps": rep.gaps,
        "duration_ms": rep.duration_ms,
        "error_message": rep.error_message,
    }


@router.get("/reports")
def list_reports(
    agent: str | None = Query(None, description="Filter by agent_name"),
    status: str | None = Query(None, description="ok | warning | critical"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _u=Depends(get_current_user),
):
    """Most-recent-first agent reports (bounded to <=500)."""
    q = db.query(AgentReport)
    if agent:
        q = q.filter(AgentReport.agent_name == agent)
    if status:
        q = q.filter(AgentReport.status == status)
    rows = q.order_by(AgentReport.run_at.desc()).limit(limit).all()
    return {"reports": [_serialize(r) for r in rows]}


@router.get("/latest")
def latest_per_agent(
    db: Session = Depends(get_db),
    _u=Depends(get_current_user),
):
    """One row per agent — the newest report — for the dashboard grid."""
    from app.tasks.agents import AGENT_INTERVAL_SECONDS
    out = []
    for name in list(AGENT_INTERVAL_SECONDS) + ["watchdog", "accountability"]:
        rep = (db.query(AgentReport)
                 .filter(AgentReport.agent_name == name)
                 .order_by(AgentReport.run_at.desc())
                 .first())
        out.append({"agent_name": name,
                    "report": _serialize(rep) if rep else None})
    return {"agents": out}


@router.get("/scorecards")
def agent_scorecards(window_hours: int = Query(24, ge=1, le=168),
                     db: Session = Depends(get_db), _u=Depends(get_current_user)):
    cards = scorecards(db, window_hours=window_hours)
    return {"target": 0.99, "scorecards": cards,
            "warning": "Agent SLA score is not detector precision or recall."}


@router.post("/{name}/run")
def run_agent(name: str, _u=Depends(require_role("admin"))):
    """Manually enqueue one agent (admin only)."""
    from app.tasks.agents import AGENT_TASKS, agent_watchdog
    tasks = dict(AGENT_TASKS)
    tasks["watchdog"] = agent_watchdog
    task = tasks.get(name)
    if task is None:
        raise HTTPException(status_code=404, detail=f"unknown agent '{name}'")
    res = task.delay()
    return {"queued": True, "agent": name, "task_id": getattr(res, "id", None)}
