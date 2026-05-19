"""User management — admin can create operators / viewers, change roles,
disable accounts, reset passwords. Operators / viewers only see their own
account via /auth/me.

Roles:
  admin     — full system access
  operator  — runs cameras, confirms/dismisses alerts
  viewer    — read-only dashboards
"""
from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.database import get_db
from app.deps import require_role
from app.models import User

router = APIRouter(prefix="/users", tags=["users"])


VALID_ROLES = ("admin", "operator", "viewer")


class UserIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    role: str = "viewer"
    is_active: bool = True


class UserPatch(BaseModel):
    full_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None      # admin password reset


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db),
               _u=Depends(require_role("admin"))):
    return db.query(User).order_by(User.id).all()


@router.post("", response_model=UserOut)
def create_user(payload: UserIn, db: Session = Depends(get_db),
                _u=Depends(require_role("admin"))):
    if payload.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of {VALID_ROLES}")
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(409, "email already in use")
    u = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name or payload.email,
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, patch: UserPatch,
                db: Session = Depends(get_db),
                actor: User = Depends(require_role("admin"))):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    if patch.role is not None:
        if patch.role not in VALID_ROLES:
            raise HTTPException(400, f"role must be one of {VALID_ROLES}")
        # Block demoting the only remaining admin.
        if u.role == "admin" and patch.role != "admin":
            other = (db.query(User)
                       .filter(User.role == "admin",
                               User.id != u.id,
                               User.is_active == True)        # noqa: E712
                       .count())
            if other == 0:
                raise HTTPException(409, "cannot demote the last active admin")
        u.role = patch.role
    if patch.full_name is not None:
        u.full_name = patch.full_name
    if patch.is_active is not None:
        u.is_active = patch.is_active
    if patch.password:
        u.password_hash = hash_password(patch.password)
    db.commit(); db.refresh(u)
    return u


@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                actor: User = Depends(require_role("admin"))):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    if u.id == actor.id:
        raise HTTPException(409, "you cannot delete your own account")
    if u.role == "admin":
        other = (db.query(User)
                   .filter(User.role == "admin",
                           User.id != u.id,
                           User.is_active == True)            # noqa: E712
                   .count())
        if other == 0:
            raise HTTPException(409, "cannot delete the last active admin")
    db.delete(u); db.commit()
    return {"deleted": user_id}
