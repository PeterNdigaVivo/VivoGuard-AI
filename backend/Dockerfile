# =====================================================================
# VivoGuard AI — backend image
# Hosts both the FastAPI HTTP server (`api` service) and the Celery
# worker pool (`worker` service). The compose file picks the command.
# =====================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System dependencies:
#   ffmpeg          — frame extraction & clip writing
#   libgl1, libglib — required by opencv-python-headless / ultralytics
#   curl            — health checks
#   build-essential — wheel fallback for some pip packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg curl libgl1 libglib2.0-0 build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so layer is cached on code-only changes.
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

COPY app    ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

EXPOSE 8000

# Default command runs the API; the `worker` service overrides this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
