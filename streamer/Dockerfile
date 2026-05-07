# Real Dockerfile (with ffmpeg apt install) lands in step 2.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY streamer ./streamer
CMD ["python", "-m", "streamer.main"]
