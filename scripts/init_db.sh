#!/usr/bin/env bash
# Run Alembic migrations and create the bootstrap admin user inside the
# api container. Invoke after `docker compose up -d` on first boot.
set -euo pipefail

docker compose exec -T api alembic upgrade head
echo "Database migrations applied."

docker compose exec -T api python -m app.scripts.bootstrap_admin
echo "Bootstrap admin ensured."
