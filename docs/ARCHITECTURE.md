# Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │                    Operators                   │
                    │           (browser, mobile-responsive)         │
                    └──────────────────────┬─────────────────────────┘
                                           │ HTTPS / WSS
                                           ▼
                    ┌────────────────────────────────────────────────┐
                    │   Nginx reverse proxy (TLS, WS upgrade, /api)  │
                    └─────────┬─────────────────────────┬────────────┘
                              │                         │
                              ▼                         ▼
                  ┌────────────────────┐   ┌────────────────────────┐
                  │ frontend (React)   │   │ backend (FastAPI)      │
                  │ Nginx static       │   │ • REST + WebSockets    │
                  └────────────────────┘   │ • JWT auth             │
                                           │ • Camera registry      │
                                           │ • Detection config     │
                                           │ • Training jobs API    │
                                           └──┬──────────┬──────────┘
                                              │          │
                            ┌─────────────────┘          └────────────────┐
                            │                                             │
                            ▼                                             ▼
              ┌─────────────────────────────┐               ┌──────────────────────┐
              │ streamer service            │               │ Celery workers       │
              │ • FFmpeg subprocess pool    │               │ • AI inference       │
              │ • Per-camera reconnect      │──── frames ──▶│ • Training jobs      │
              │ • DDNS resolver             │   (Redis)     │ • Maintenance        │
              └─────────────────────────────┘               └──────┬───────────────┘
                                                                    │ events
                                                                    ▼
                                                          ┌────────────────────┐
                                                          │ Alert engine       │
                                                          │ • SMTP / Twilio /  │
                                                          │   Webhook          │
                                                          └────────────────────┘

              Persistent stores: PostgreSQL (relational), Redis (queue + buffer),
                                 MinIO (clips, thumbnails, model weights, datasets).
```

## Data flow per camera

```
RTSP source ──▶ FFmpeg decode ──▶ Redis frame buffer (latest-N ring)
                                          │
                                          ▼
                                AI inference worker
                              (YOLOv8 + detector chain
                               + zone polygon evaluator)
                                          │
                                          ├──▶ HLS segment writer ──▶ MinIO (clips)
                                          ├──▶ Thumbnail writer  ──▶ MinIO (thumbs)
                                          └──▶ DetectionEvent ───▶ Postgres
                                                       │
                                                       ▼
                                                Alert engine
                                                       │
                                                       ▼
                                       SMTP / Twilio / Webhook
                                       + WebSocket push to UI
```

See `ROADMAP.md` for build status and `DAHUA_WAN_EXAMPLE.md` for an
end-to-end walkthrough connecting a remote NVR.
