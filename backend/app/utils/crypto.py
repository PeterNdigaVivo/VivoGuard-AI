"""Symmetric encryption for camera credentials at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key from CREDENTIALS_FERNET_KEY.
If the key is empty (dev), values are stored base64-encoded plaintext as a
fallback so the app still boots — never deploy that way.
"""
from __future__ import annotations
import base64
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet | None:
    if not settings.credentials_fernet_key:
        return None
    return Fernet(settings.credentials_fernet_key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a string for storage. Empty input → empty output."""
    if not plaintext:
        return ""
    f = _fernet()
    if f is None:
        # Plain b64 placeholder — clearly not secure, intentionally readable.
        return "PLAIN:" + base64.urlsafe_b64encode(plaintext.encode()).decode()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Reverse `encrypt`."""
    if not ciphertext:
        return ""
    if ciphertext.startswith("PLAIN:"):
        return base64.urlsafe_b64decode(ciphertext[len("PLAIN:"):]).decode()
    f = _fernet()
    if f is None:
        raise RuntimeError("CREDENTIALS_FERNET_KEY missing but ciphertext present")
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Cannot decrypt — wrong CREDENTIALS_FERNET_KEY?") from e
