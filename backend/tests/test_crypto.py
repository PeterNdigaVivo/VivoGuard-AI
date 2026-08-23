"""Credential-at-rest security regression tests."""
from cryptography.fernet import Fernet
import pytest

from app.utils import crypto


def test_production_requires_fernet_key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "app_env", "production")
    monkeypatch.setattr(crypto.settings, "credentials_fernet_key", "")

    with pytest.raises(RuntimeError, match="required"):
        crypto.encrypt("camera-password")


def test_production_rejects_legacy_plaintext_value(monkeypatch):
    monkeypatch.setattr(crypto.settings, "app_env", "production")
    monkeypatch.setattr(crypto.settings, "credentials_fernet_key", Fernet.generate_key().decode())

    with pytest.raises(RuntimeError, match="forbidden"):
        crypto.decrypt("PLAIN:Y2FtZXJhLXBhc3N3b3Jk")


def test_development_fallback_round_trip(monkeypatch):
    monkeypatch.setattr(crypto.settings, "app_env", "development")
    monkeypatch.setattr(crypto.settings, "credentials_fernet_key", "")

    encrypted = crypto.encrypt("camera-password")

    assert encrypted.startswith("PLAIN:")
    assert crypto.decrypt(encrypted) == "camera-password"


def test_fernet_round_trip_in_production(monkeypatch):
    monkeypatch.setattr(crypto.settings, "app_env", "production")
    monkeypatch.setattr(crypto.settings, "credentials_fernet_key", Fernet.generate_key().decode())

    encrypted = crypto.encrypt("camera-password")

    assert not encrypted.startswith("PLAIN:")
    assert crypto.decrypt(encrypted) == "camera-password"


def test_invalid_fernet_key_has_actionable_error(monkeypatch):
    monkeypatch.setattr(crypto.settings, "app_env", "production")
    monkeypatch.setattr(crypto.settings, "credentials_fernet_key", "not-a-key")

    with pytest.raises(RuntimeError, match="valid Fernet key"):
        crypto.encrypt("camera-password")
