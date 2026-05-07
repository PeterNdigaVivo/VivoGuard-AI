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

# 3. Build images. The streamer image FROMs the api image, so build api first.
echo "→ building api image (this is the slow one — torch + ultralytics)"
docker compose build api

echo "→ building remaining images"
docker compose build streamer worker frontend

# 4. Bring everything up.
echo "→ starting all services"
docker compose up -d

# 5. Wait for postgres health, then migrate.
echo "→ waiting for postgres health"
for i in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U "$(grep POSTGRES_USER .env | cut -d= -f2)" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "→ running alembic migrations"
docker compose exec -T api alembic upgrade head

# 6. (Optional) pre-download base YOLOv8 weights so the first inference run
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
