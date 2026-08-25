import pytest

from app.config import Settings, validate_production_security


def _settings(**overrides):
    values = {
        "app_env": "production",
        "app_debug": False,
        "jwt_secret": "a-production-only-random-secret-1234567890",
        "bootstrap_admin_password": "a-unique-bootstrap-password",
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
