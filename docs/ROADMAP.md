# Build roadmap

The repository is being built across 15 steps. Each step lands as one or
more commits on this branch. This file is the source of truth for what is
implemented vs. stubbed.

| #  | Deliverable                                          | Status   |
|----|------------------------------------------------------|----------|
| 1  | Project folder structure                             | done     |
| 2  | Docker Compose + Nginx config                        | done     |
| 3  | DB models (SQLAlchemy) + Alembic migrations          | done     |
| 4  | Camera connection engine (RTSP / ISAPI / ONVIF/ WAN) | done     |
| 5  | Stream manager (FFmpeg + Redis frame buffer)         | pending  |
| 6  | AI inference worker (YOLOv8 + detectors + zones)     | pending  |
| 7  | Alert engine + notification pipeline                 | pending  |
| 8  | Detection config API (toggles + zone drawing)        | pending  |
| 9  | AI Training Studio backend                           | pending  |
| 10 | AI Training Studio frontend                          | pending  |
| 11 | VMS frontend: Camera management page                 | pending  |
| 12 | VMS frontend: Live view grid with AI overlays        | pending  |
| 13 | VMS frontend: Alerts page                            | pending  |
| 14 | VMS frontend: System health page                     | pending  |
| 15 | README + Dahua WAN end-to-end example                | pending  |

## Architectural decisions (locked at start of build)

- **Vendor SDKs:** RTSP / ISAPI (Hikvision) / HTTP-CGI (Dahua) / ONVIF only.
  No proprietary C SDK blobs are committed. The `vendor_sdk/` directory
  exists as an optional plug-in slot for future ctypes wrappers but is not
  on the default code path.
- **Auth & tenancy:** single-tenant. JWT login. Roles: admin / operator /
  viewer. `org_id` is **not** present on tables.
- **Frontend:** Vite + React + TypeScript + Tailwind + shadcn/ui, heavily
  commented for readability over abstraction.
- **Notifications:** pluggable channel interface; SMTP, Twilio SMS, and
  generic outbound webhook all wired. A channel with missing credentials
  is silently inactive.
