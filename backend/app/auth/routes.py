"""Auth endpoints: /auth/login, /auth/me, /auth/refresh."""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password, issue_token, decode_token
from app.database import get_db
from app.deps import get_current_user
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None
    role: str
    is_active: bool


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return TokenOut(
        access_token=issue_token(str(user.id), user.role, kind="access"),
        refresh_token=issue_token(str(user.id), user.role, kind="refresh"),
        role=user.role,
    )


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenOut)
def refresh(payload: RefreshIn, db: Session = Depends(get_db)) -> TokenOut:
    data = decode_token(payload.refresh_token)
    if not data or data.get("kind") != "refresh":
        raise HTTPException(401, "invalid refresh token")
    user = db.get(User, int(data["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "user disabled")
    return TokenOut(
        access_token=issue_token(str(user.id), user.role, kind="access"),
        refresh_token=issue_token(str(user.id), user.role, kind="refresh"),
        role=user.role,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email, full_name=user.full_name,
                   role=user.role, is_active=user.is_active)


def ensure_bootstrap_admin(db: Session, email: str, password: str) -> None:
    """Called during startup; creates the first admin if no users exist."""
    if db.query(User).count() > 0:
        return
    admin = User(
        email=email,
        password_hash=hash_password(password),
        full_name="Bootstrap Admin",
        role="admin",
        is_active=True,
    )
    db.add(admin)
    db.commit()
