"""Auth endpoints: /auth/login, /auth/me, /auth/refresh."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password, issue_token, decode_token
from app.database import get_db
from app.deps import get_current_user
from app.models import User

log = logging.getLogger(__name__)

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
    # IMPORTANT: the HTTP response stays a generic "invalid credentials" to
    # avoid leaking which accounts exist. But we log the precise reason
    # server-side so the operator can diagnose 401s from `docker compose
    # logs api`. Nothing sensitive is logged.
    user = db.query(User).filter(User.email == payload.email).first()
    generic = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if user is None:
        log.warning("login denied: no user with email=%s", payload.email)
        raise generic
    if not user.is_active:
        log.warning("login denied: user id=%s email=%s is inactive", user.id, user.email)
        raise generic
    if not user.password_hash:
        log.warning("login denied: user id=%s has empty password_hash (re-run reset_admin)",
                    user.id)
        raise generic
    # Sanity-check the stored hash format. bcrypt hashes always start
    # $2a$ / $2b$ / $2y$; a non-bcrypt value here means a previous broken
    # passlib install wrote junk into the row.
    if not user.password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        log.warning(
            "login denied: user id=%s password_hash is not a bcrypt hash "
            "(prefix=%r). Run: python -m app.scripts.reset_admin --email %s --password ...",
            user.id, user.password_hash[:4], user.email,
        )
        raise generic
    if not verify_password(payload.password, user.password_hash):
        log.warning("login denied: bcrypt verify failed for user id=%s", user.id)
        raise generic

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
