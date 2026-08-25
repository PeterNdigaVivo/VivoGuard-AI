import pytest

from app.config import Settings
from app.startup import upgrade_database


class _Config:
    def __init__(self, path):
        self.path = path


def _settings(environment: str) -> Settings:
    return Settings(
        app_env=environment,
        jwt_secret="a-production-only-random-secret-1234567890",
        bootstrap_admin_password="a-unique-bootstrap-password",
    )


def test_startup_migrates_to_head_before_serving():
    calls = []

    def upgrade(config, revision):
        calls.append((config.path, revision))

    assert upgrade_database(
        _settings("production"), config_factory=_Config, upgrade=upgrade,
    ) is True
    assert calls == [("alembic.ini", "head")]


def test_production_refuses_to_start_when_migration_fails():
    def fail(_config, _revision):
        raise ConnectionError("database unavailable")

    with pytest.raises(RuntimeError, match="refusing to start production API"):
        upgrade_database(
            _settings("production"), config_factory=_Config, upgrade=fail,
        )


def test_development_keeps_warning_only_migration_behaviour(caplog):
    def fail(_config, _revision):
        raise ConnectionError("database unavailable")

    assert upgrade_database(
        _settings("development"), config_factory=_Config, upgrade=fail,
    ) is False
    assert "skipped outside production" in caplog.text
