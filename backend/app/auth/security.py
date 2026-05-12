"""Password hashing + JWT issuing/validation.

Uses the bcrypt library directly — no passlib indirection.

Why direct bcrypt:
  - passlib 1.7.x reads `bcrypt.__about__.__version__` which bcrypt 4.1+
    removed, breaking every login with a misleading
    "ValueError: password cannot be longer than 72 bytes" error.
  - Pinning bcrypt below 4.1 was a fragile workaround that pip's
    resolver kept undoing in some build environments.
  - Direct bcrypt usage is supported by every version (3.x, 4.x, 5.x)
    and is one less moving part.

Hash format is the standard `$2b$<rounds>$<salt+digest>` modular crypt
form — *byte-compatible* with hashes that passlib previously produced,
so existing users' password_hash rows continue to verify.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import jwt, JWTError

from app.config import settings


# bcrypt's protocol-level limit is 72 bytes of password. Anything beyond
# is silently ignored by the algorithm itself; we truncate explicitly so
# the behaviour is documented and deterministic, and so any future
# wrapper that *rejects* > 72-byte inputs (as bcrypt 4.1+ does) is fed
# input it accepts.
_BCRYPT_MAX_BYTES = 72


def _prep(plain: str) -> bytes:
    """UTF-8-encode the password and truncate to bcrypt's 72-byte limit."""
    if not isinstance(plain, str):
        plain = str(plain or "")
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str, *, rounds: int = 12) -> str:
    """Produce a bcrypt hash. Returns the canonical `$2b$...` string."""
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(_prep(plain), salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time compare. Returns False on any decode/version error
    instead of raising, so a malformed hash row can't crash login."""
    if not hashed:
        return False
    try:
        # hashed comes back from Postgres as `str`; bcrypt wants bytes.
        return bcrypt.checkpw(_prep(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---- JWT (unchanged) ------------------------------------------------------

def issue_token(sub: str, role: str, *, kind: str = "access") -> str:
    now = datetime.now(timezone.utc)
    if kind == "access":
        ttl = timedelta(minutes=settings.jwt_access_ttl_minutes)
    else:
        ttl = timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub":  sub,
        "role": role,
        "kind": kind,
        "iat":  int(now.timestamp()),
        "exp":  int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
