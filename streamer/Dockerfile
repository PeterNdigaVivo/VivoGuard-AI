# =====================================================================
# Streamer image (~200 MB)
#
# FROMs the slim API image so it inherits the app.* package without
# pulling in torch/ultralytics/cv2. The streamer only needs ffmpeg
# (already in the base) and Redis client libs.
# =====================================================================
FROM vivoguard/api:latest

WORKDIR /app
COPY streamer ./streamer

CMD ["python", "-m", "streamer.main"]
