"""Symmetric encryption for camera credentials at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key from CREDENTIALS_FERNET_KEY.
Development and test environments may use a clearly marked base64 fallback.
Production fails closed when the key is missing or a legacy plaintext value
is encountered.
"""
from __future__ import annotations
import base64
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet | None:
    if not settings.credentials_fernet_key:
        return None
    try:
        return Fernet(settings.credentials_fernet_key.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CREDENTIALS_FERNET_KEY is not a valid Fernet key") from exc


def _plaintext_fallback_allowed() -> bool:
    return settings.app_env.strip().lower() in {
        "dev", "development", "local", "test", "testing",
    }


def encrypt(plaintext: str) -> str:
    """Encrypt a string for storage. Empty input → empty output."""
    if not plaintext:
        return ""
    f = _fernet()
    if f is None:
        if not _plaintext_fallback_allowed():
            raise RuntimeError(
                "CREDENTIALS_FERNET_KEY is required outside development/test"
            )
        # Plain b64 placeholder — clearly not secure, intentionally readable.
        return "PLAIN:" + base64.urlsafe_b64encode(plaintext.encode()).decode()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Reverse `encrypt`."""
    if not ciphertext:
        return ""
    if ciphertext.startswith("PLAIN:"):
        if not _plaintext_fallback_allowed():
            raise RuntimeError(
                "Plaintext-encoded credentials are forbidden outside "
                "development/test"
            )
        try:
            return base64.urlsafe_b64decode(
                ciphertext[len("PLAIN:"):],
            ).decode()
        except Exception as exc:
            raise RuntimeError("Malformed plaintext credential encoding") from exc
    f = _fernet()
    if f is None:
        raise RuntimeError("CREDENTIALS_FERNET_KEY missing but ciphertext present")
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Cannot decrypt — wrong CREDENTIALS_FERNET_KEY?") from e
