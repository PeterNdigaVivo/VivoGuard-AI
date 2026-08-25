#!/usr/bin/env bash
# =====================================================================
# VivoGuard AI — one-shot deploy script
# Run this on the target server after cloning the repo.
# =====================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

# 1. Sanity-check prerequisites.
command -v docker >/dev/null         || { echo "docker not found"; exit 1; }
docker compose version >/dev/null    || { echo "docker compose plugin not found"; exit 1; }
[ -f .env.example ]                  || { echo ".env.example missing — wrong dir?"; exit 1; }

# 2. Generate .env if missing. Strong secrets are minted automatically.
if [ ! -f .env ]; then
  echo "→ generating .env"
  cp .env.example .env

  FERNET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
           || docker run --rm python:3.11-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
  JWT=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))" 2>/dev/null \
        || docker run --rm python:3.11-slim python -c "import secrets; print(secrets.token_urlsafe(48))")
  PG_PW=$(openssl rand -base64 24 | tr -d '/=+')
  S3_PW=$(openssl rand -base64 24 | tr -d '/=+')

  sed -i "s|^CREDENTIALS_FERNET_KEY=.*|CREDENTIALS_FERNET_KEY=${FERNET}|" .env
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${JWT}|"                            .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PW}|"            .env
  sed -i "s|^S3_SECRET_KEY=.*|S3_SECRET_KEY=${S3_PW}|"                    .env

  echo
  echo "Edit .env to set BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD,"
  echo "USE_GPU, SMTP_*, TWILIO_*, WEBHOOK_*, then re-run this script."
  exit 0
fi

# 3. Build images. Build every named application service explicitly so a new
# queue/worker cannot be omitted from a release by an obsolete service alias.
echo "→ building api image"
docker compose build api

echo "→ building remaining images"
docker compose build streamer worker-inference worker-alerts worker-training recorder frontend

# 4. Start only stateful dependencies.  Keep the currently running API and
# workers on the old image while the new schema is applied; migrations in a
# release must remain backwards-compatible for this short overlap window.
echo "→ starting database and queue"
docker compose up -d postgres redis

# 5. Wait for postgres health, then migrate with the newly built API image.
echo "→ waiting for postgres health"
for i in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U "$(grep POSTGRES_USER .env | cut -d= -f2)" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "→ running alembic migrations"
docker compose run --rm --no-deps api alembic upgrade head

# 6. Recreate the application tier only after the schema is ready.  Nginx
# resolves Compose service names at startup, so refresh it after an API
# container replacement to avoid a stale upstream address and public 502s.
echo "→ starting application services"
docker compose up -d
docker compose restart nginx

echo "→ waiting for API health"
for i in $(seq 1 60); do
  if docker compose exec -T api curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "API failed to become healthy"
    docker compose ps
    exit 1
  fi
  sleep 2
done

echo "→ verifying nginx-to-API routing"
for i in $(seq 1 20); do
  # Use the loopback IPv4 address because BusyBox may resolve localhost to
  # ::1 while nginx only listens on IPv4.  The TLS profile redirects port 80
  # to its self-signed loopback hostname, so certificate verification must be
  # disabled for this container-internal probe.  The public certificate is
  # verified separately by the external production check.
  if docker compose exec -T nginx wget --no-check-certificate -qO- \
       http://127.0.0.1/api/healthz \
       | grep -q '"status":"ok"'; then
    break
  fi
  if [ "$i" -eq 20 ]; then
    echo "nginx cannot reach the healthy API"
    docker compose logs --tail=100 nginx api
    exit 1
  fi
  sleep 1
done

# A healthy API does not prove that camera inference recovered after the
# application tier was recreated.  In particular, restarting the streamer can
# temporarily empty the frame buffer and create a fleet-wide observation gap.
# Do not declare the release complete until the authoritative supervisor
# breadcrumb is fresh, every latency-critical camera is inside its SLA, and at
# least one worker is active whenever fresh camera frames exist.
echo "→ waiting for inference coverage recovery"
INFERENCE_RECOVERY_TIMEOUT_SECONDS=${DEPLOY_INFERENCE_RECOVERY_TIMEOUT_SECONDS:-600}
INFERENCE_RECOVERY_POLL_SECONDS=${DEPLOY_INFERENCE_RECOVERY_POLL_SECONDS:-5}
INFERENCE_RECOVERY_DEADLINE=$((SECONDS + INFERENCE_RECOVERY_TIMEOUT_SECONDS))
while true; do
  INFERENCE_HEALTH_JSON=$(
    docker compose exec -T redis redis-cli --raw GET vg:inference:health \
      2>/dev/null || true
  )
  if [ -n "$INFERENCE_HEALTH_JSON" ] && printf '%s' "$INFERENCE_HEALTH_JSON" \
      | docker compose exec -T api python -c '
import json
import sys
import time

try:
    health = json.load(sys.stdin)
    age = time.time() - float(health["last_run_ts"])
    overdue = health["critical_cameras_overdue"]
    fresh = int(health.get("cameras_fresh") or 0)
    active = int(health.get("cameras_actively_inferencing") or 0)
    healthy = (
        age <= 120
        and overdue is not None
        and int(overdue) == 0
        and (fresh == 0 or active > 0)
    )
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    healthy = False
sys.exit(0 if healthy else 1)
'; then
    break
  fi
  if [ "$SECONDS" -ge "$INFERENCE_RECOVERY_DEADLINE" ]; then
    echo "inference coverage failed to recover after deploy"
    if [ -n "$INFERENCE_HEALTH_JSON" ]; then
      printf 'latest inference health: %s\n' "$INFERENCE_HEALTH_JSON"
    else
      echo "latest inference health: unavailable"
    fi
    docker compose ps
    docker compose logs --tail=100 streamer worker-inference
    exit 1
  fi
  sleep "$INFERENCE_RECOVERY_POLL_SECONDS"
done

# 7. (Optional) pre-download base YOLOv8 weights so the first inference run
# isn't blocked on a network fetch.
echo "→ pre-downloading YOLOv8 base weights"
mkdir -p data/models
for w in yolov8n.pt yolov8s.pt; do
  if [ ! -f "data/models/$w" ]; then
    curl -fL -o "data/models/$w" "https://github.com/ultralytics/assets/releases/download/v8.3.0/$w" || true
  fi
done

echo
echo "=================================================================="
echo "  Deploy complete."
echo "  UI:        http://$(hostname -I | awk '{print $1}')/"
echo "  API docs:  http://$(hostname -I | awk '{print $1}')/api/docs"
echo "  Sign in:   $(grep BOOTSTRAP_ADMIN_EMAIL .env | cut -d= -f2)"
echo "             (password is in .env: BOOTSTRAP_ADMIN_PASSWORD)"
echo "=================================================================="
