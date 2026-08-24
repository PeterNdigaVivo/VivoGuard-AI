from pathlib import Path


COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def test_compose_passes_all_odoo_pull_settings_to_app_services() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    required = {
        "ODOO_SYNC_ENABLED",
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_USER",
        "ODOO_API_KEY",
        "ODOO_MASTER_CRON",
        "ODOO_TXN_MINUTES",
        "ODOO_HOURS_MAX_AGE_HOURS",
        "ODOO_ROSTER_MAX_AGE_HOURS",
        "ODOO_ROSTER_RETENTION_DAYS",
        "ODOO_REQUEST_TIMEOUT_SECONDS",
        "ODOO_PAGE_SIZE",
        "ODOO_CONVERSION_MAX",
        "ODOO_CHANGING_ROOM_GRACE_MINUTES",
        "ODOO_CIRCUIT_FAILURES",
        "ODOO_CIRCUIT_COOLDOWN_MINUTES",
    }

    missing = sorted(name for name in required if f"  {name}: ${{{name}" not in compose)
    assert not missing, f"Odoo settings missing from x-app-env: {missing}"


def test_compose_passes_provider_failover_settings_to_workers() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    required = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AGENTS_LLM_ENABLED",
        "AGENTS_LLM_PROVIDER",
        "AGENTS_LLM_FALLBACK_PROVIDER",
        "AGENTS_LLM_MODEL",
        "AGENTS_LLM_OPENAI_MODEL",
        "AGENTS_LLM_TIMEOUT_SECONDS",
    }

    missing = sorted(name for name in required if f"  {name}: ${{{name}" not in compose)
    assert not missing, f"LLM failover settings missing from x-app-env: {missing}"
