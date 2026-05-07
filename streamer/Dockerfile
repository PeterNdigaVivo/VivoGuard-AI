# =====================================================================
# VivoGuard AI — standalone streamer service
#
# Built FROM the backend image so all app.* Python modules (config,
# database, models, stream/, utils/) are already installed. The streamer
# only adds its own thin entrypoint and reuses the rest.
#
# IMPORTANT: build the backend image first so this FROM resolves:
#   docker compose build api && docker compose build streamer
# =====================================================================
FROM vivoguard/api:latest

WORKDIR /app
# Copy the streamer entrypoint package alongside backend's app/.
COPY streamer ./streamer

CMD ["python", "-m", "streamer.main"]
