"""Password hashing + JWT issuing/validation."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
from passlib.context import CryptContext
from jose import jwt, JWTError

from app.config import settings


_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


def issue_token(sub: str, role: str, *, kind: str = "access") -> str:
    now = datetime.now(timezone.utc)
    if kind == "access":
        ttl = timedelta(minutes=settings.jwt_access_ttl_minutes)
    else:
        ttl = timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub": sub,
        "role": role,
        "kind": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
