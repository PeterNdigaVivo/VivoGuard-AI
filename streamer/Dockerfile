# =====================================================================
# Streamer image (~200 MB)
#
# FROMs the slim API image for its runtime dependencies without pulling in
# torch/ultralytics/cv2.  Copy app.* from the same source checkout as the
# streamer entrypoint: relying on the mutable local api:latest contents made
# streamer-only builds able to combine new streamer code with stale backend
# dataclasses.
# =====================================================================
FROM vivoguard/api:latest

WORKDIR /app
COPY backend/app ./app
COPY streamer ./streamer

CMD ["python", "-m", "streamer.main"]
