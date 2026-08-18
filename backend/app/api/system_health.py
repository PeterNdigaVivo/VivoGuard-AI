"""GET /system-health — mission-control snapshot, SYSTEM ADMINS ONLY.

Not the same audience as the existing /system/health (any authenticated
user, powers the System page): this endpoint is restricted to the
three platform operators in app.utils.system_admins and returns the
full infrastructure snapshot (containers, storage, training pipeline,
integrations) that also feeds the 08:00 EAT daily email.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.utils.system_admins import is_system_admin
from app.utils.system_health import collect_system_health, overall_status

log = logging.getLogger(__name__)

router = APIRouter(prefix="/system-health", tags=["system-health"])


def require_system_admin(user: User = Depends(get_current_user)) -> User:
    """403 unless the user's email is in SYSTEM_ADMIN_EMAILS. Role is
    irrelevant here — regular admins are refused by design."""
    if not is_system_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "system-admin access required")
    return user


@router.get("")
def system_health(db: Session = Depends(get_db),
                  _u: User = Depends(require_system_admin)):
    import traceback
    try:
        snap = collect_system_health(db)
        emoji, label = overall_status(snap)
        snap["overall"] = {"emoji": emoji, "label": label}
        return snap
    except HTTPException:
        raise
    except Exception as e:
        # Full traceback in the api logs + the message in `detail` so a
        # 500 is never silent (same pattern as the cross-store fix).
        log.error("system_health failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
