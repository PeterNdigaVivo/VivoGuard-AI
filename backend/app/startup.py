"""Fail-safe application startup checks."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable

from alembic import command
from alembic.config import Config

from app.config import Settings


log = logging.getLogger(__name__)


def upgrade_database(
    candidate: Settings,
    *,
    config_factory: Callable[[str], Config] = Config,
    upgrade: Callable[[Config, str], None] = command.upgrade,
) -> bool:
    """Apply every pending migration before the API accepts traffic.

    Production fails closed on any migration error. Continuing with a newer
    application image against an older schema creates harder-to-diagnose data
    loss and partial outages than a deliberate unhealthy container. Developer
    environments retain the previous warning-only behaviour for lightweight
    offline work.
    """
    cfg_path = "/app/alembic.ini" if os.path.exists("/app/alembic.ini") else "alembic.ini"
    try:
        upgrade(config_factory(cfg_path), "head")
    except Exception as exc:
        if candidate.app_env.strip().lower() in {"production", "prod"}:
            raise RuntimeError(
                "database migration failed; refusing to start production API"
            ) from exc
        log.warning("alembic upgrade skipped outside production: %s", exc)
        return False
    log.info("alembic upgrade head: complete")
    return True
