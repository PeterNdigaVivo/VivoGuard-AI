"""Shared FastAPI dependencies — DB session, current user, role guards."""
from __future__ import annotations
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.security import decode_token
from app.database import get_db
from app.models import User

# tokenUrl is informational for Swagger; real login is at /auth/login.
_oauth2 = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def get_current_user(token: str | None = Depends(_oauth2),
                     db: Session = Depends(get_db)) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    data = decode_token(token)
    if not data or data.get("kind") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    user = db.get(User, int(data["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user


def require_role(*roles: str):
    """Factory: dependency that requires the current user have one of `roles`.
    Admins always pass."""
    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles and user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user
    return _dep
