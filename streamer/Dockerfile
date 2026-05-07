# =====================================================================
# VivoGuard AI — standalone streamer service
# Owns one FFmpeg subprocess per active camera; pushes decoded frames
# into Redis. Runs independently so a streamer crash never takes down
# the FastAPI control plane.
# =====================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg is mandatory; libgl/glib are pulled in by opencv (used for resize).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY streamer ./streamer

CMD ["python", "-m", "streamer.main"]
