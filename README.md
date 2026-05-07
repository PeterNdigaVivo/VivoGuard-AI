# VivoGuard AI

Production-ready AI video surveillance platform supporting on-premise NVRs,
LAN IP cameras, WAN/remote IP cameras (Dahua, Hikvision, ONVIF, generic
RTSP), and a built-in AI Training Studio for custom YOLOv8 models.

> **Status:** the repository layout, infrastructure, schema, APIs, stream
> pipeline, AI inference, training studio, and React frontend are built up
> incrementally across the steps in `docs/ROADMAP.md`. See that file for
> what is currently wired vs. stubbed.

## Quick links

- [Architecture](docs/ARCHITECTURE.md)
- [Hardware sizing](docs/HARDWARE_SIZING.md)
- [Connecting a Dahua NVR over WAN (end-to-end example)](docs/DAHUA_WAN_EXAMPLE.md)
- [Roadmap / build log](docs/ROADMAP.md)

## Repository layout

```
backend/         FastAPI app: API, auth, models, AI workers, training, alerts
streamer/        Standalone FFmpeg subprocess pool service
frontend/        Vite + React + TypeScript + Tailwind + shadcn/ui dashboard
deploy/          Nginx config, TLS material
docs/            Architecture, hardware sizing, vendor walkthroughs
data/            Mounted runtime data: recordings, models, datasets, thumbs
vendor_sdk/      (Optional, gitignored) drop-in vendor C SDK shared objects
scripts/         Bootstrap helpers
docker-compose.yml
.env.example
```

The full installation guide lands in step 15 of the build (see ROADMAP).
