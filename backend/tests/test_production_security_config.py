import pytest

from app.config import Settings, validate_production_security


def _settings(**overrides):
    values = {
        "app_env": "production",
        "app_debug": False,
        "jwt_secret": "a-production-only-random-secret-1234567890",
        "bootstrap_admin_password": "a-unique-bootstrap-password",
        "postgres_password": "a-unique-database-password",
        "database_url": "",
    }
    values.update(overrides)
    return Settings(**values)


def test_secure_production_authentication_configuration_passes():
    validate_production_security(_settings())


@pytest.mark.parametrize("overrides, expected", [
    ({"jwt_secret": "dev-only-change-me"}, "JWT_SECRET"),
    ({"jwt_secret": "too-short-for-production"}, "JWT_SECRET"),
    ({"bootstrap_admin_password": "change-me-now"},
     "BOOTSTRAP_ADMIN_PASSWORD"),
    ({"app_debug": True}, "APP_DEBUG"),
    ({"postgres_password": "vivoguard"}, "database password"),
    ({"postgres_password": "change-me"}, "database password"),
    ({"database_url":
        "postgresql+psycopg://vivoguard:vivoguard@postgres:5432/vivoguard"},
     "database password"),
])
def test_insecure_production_authentication_configuration_fails_closed(
    overrides, expected,
):
    with pytest.raises(RuntimeError, match=expected):
        validate_production_security(_settings(**overrides))


def test_development_configuration_keeps_documented_defaults_available():
    validate_production_security(Settings(
        app_env="development",
        jwt_secret="dev-only-change-me",
        bootstrap_admin_password="change-me-now",
        app_debug=True,
    ))


def test_database_url_override_with_strong_password_passes():
    # POSTGRES_PASSWORD is unused when DATABASE_URL is set, so a leftover
    # default there must not block startup.
    validate_production_security(_settings(
        postgres_password="vivoguard",
        database_url=(
            "postgresql+psycopg://vg:s%40fe-Pass@db.internal:5432/vivoguard"),
    ))


def test_error_message_never_contains_the_password():
    with pytest.raises(RuntimeError) as e:
        validate_production_security(_settings(postgres_password="change-me"))
    assert "change-me" not in str(e.value)
