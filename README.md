# VivoGuard AI

AI-powered video surveillance platform supporting on-premise NVRs, LAN IP
cameras, WAN/remote cameras (Dahua, Hikvision, ONVIF, generic RTSP), and
a built-in AI Training Studio for custom YOLOv8 models.

## Features at a glance

- **Universal device connector** — Dahua NVRs (HTTP/CGI + RTSP), Hikvision
  NVRs (ISAPI + RTSP), ONVIF cameras, generic RTSP cameras, and WAN
  variants of all of the above (public IP + port forwarding, DDNS).
- **20 detection types** out of the box: person, vehicle, animal, LPR,
  face, weapon, weapon brandished, crowd, trespass, tripwire, tailgating,
  loitering, fall, smoke, fire, abandoned object, shelf, occupancy,
  heatmap, custom-trained models.
- **Per-camera AI configuration** — toggles, confidence sliders, dwell
  times, schedules, polygon zones, tripwire lines.
- **AI Training Studio** — capture frames from cameras, upload datasets,
  draw bounding boxes (with auto-suggest), train YOLOv8 models, watch
  live progress, evaluate, deploy to cameras, export to TorchScript /
  ONNX / TensorRT, roll back, and a continuous-learning feedback loop
  driven by alert confirm/dismiss actions.
- **Live VMS** — multi-tile grid (1×1 → 4×4), per-tile AI bbox overlay,
  alerts feed with real-time WebSocket push, system health dashboard.
- **Alert pipeline** — pluggable SMTP / Twilio SMS / outbound webhook
  channels; channels with missing creds stay silently inactive.
- **Production-ready** — single `docker compose up -d` brings up
  FastAPI + Celery worker + streamer + frontend + Postgres + Redis +
  MinIO behind Nginx (HTTPS-ready).

## Quick links

| Document                                               | Purpose                                  |
|--------------------------------------------------------|------------------------------------------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)         | System diagram + data flow per camera    |
| [`docs/HARDWARE_SIZING.md`](docs/HARDWARE_SIZING.md)   | Cameras vs CPU / RAM / GPU / storage     |
| [`docs/DAHUA_WAN_EXAMPLE.md`](docs/DAHUA_WAN_EXAMPLE.md)| Step-by-step remote NVR connection      |
| [`docs/ROADMAP.md`](docs/ROADMAP.md)                   | What was built in which commit          |

## Repository layout

```
backend/         FastAPI app: API, auth, models, AI workers, training, alerts
streamer/        Standalone FFmpeg subprocess pool service
frontend/        Vite + React + TypeScript + Tailwind dashboard
deploy/          Nginx config, TLS material
docs/            Architecture, hardware sizing, vendor walkthroughs
data/            Mounted runtime data: recordings, models, datasets, thumbs
vendor_sdk/      (Optional, gitignored) drop-in vendor C SDK shared objects
scripts/         Bootstrap helpers
docker-compose.yml
.env.example
```

---

# 1. Install — Ubuntu 22.04

These steps assume a clean Ubuntu 22.04 server, root or sudo access.

## 1.1 System prerequisites

```bash
sudo apt update && sudo apt -y install \
  ca-certificates curl gnupg lsb-release git
```

## 1.2 Docker Engine + Compose

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
   https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
```

## 1.3 NVIDIA driver + container toolkit (GPU only)

Skip this section if you're running CPU-only inference (set `USE_GPU=false`
in `.env`).

```bash
sudo ubuntu-drivers autoinstall
sudo reboot

# After reboot:
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt -y install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

Then uncomment the `<<: *gpu-reservation` line under the `worker:` (and
optionally `api:`) services in `docker-compose.yml`.

## 1.4 Clone and configure

```bash
git clone https://github.com/PeterNdigaVivo/VivoGuard-AI.git
cd VivoGuard-AI

cp .env.example .env

# Generate the two required secrets (Fernet key for camera passwords + JWT secret):
python3 -c "from cryptography.fernet import Fernet; print('CREDENTIALS_FERNET_KEY=' + Fernet.generate_key().decode())" >> .env
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env

# Edit the rest:
${EDITOR:-nano} .env
```

Minimum fields to set in `.env`:

- `POSTGRES_PASSWORD`, `S3_SECRET_KEY` — strong random values.
- `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` — first user that
  will be auto-created on first boot.
- `USE_GPU` — `true` for GPU, `false` for CPU.

## 1.5 Pre-download YOLOv8 base weights (recommended)

```bash
./scripts/download_models.sh
```

## 1.6 Bring it up

```bash
# Build images. The streamer image inherits FROM the api image so build
# the api first.
docker compose build api
docker compose build

docker compose up -d
docker compose ps
```

Run the database migrations and create the bootstrap admin:

```bash
./scripts/init_db.sh
```

Open the UI at **http://your-server-ip/** and sign in with the bootstrap
admin credentials.

## 1.7 (Optional) HTTPS

Drop a TLS cert/key into `deploy/ssl/`:

```
deploy/ssl/cert.pem
deploy/ssl/key.pem
```

Reload nginx: `docker compose restart nginx`. The HTTPS server block in
`deploy/nginx/nginx.conf` is already wired up to use those files.

---

# 2. Daily operations

```bash
# Tail logs
docker compose logs -f api worker streamer

# Apply schema migrations after pulling
docker compose exec -T api alembic upgrade head

# Wipe all data and start fresh
docker compose down -v
```

---

# 3. End-to-end example: connect a Dahua NVR over WAN

The dedicated walkthrough lives in
[`docs/DAHUA_WAN_EXAMPLE.md`](docs/DAHUA_WAN_EXAMPLE.md). The short
version:

1. On the router behind the NVR, forward TCP **80** (HTTP), **554**
   (RTSP), and optionally **7000** (Dahua server port — informational
   only; this build does not use the C SDK) to the NVR's LAN IP.
2. From a host with `ffprobe` installed, sanity-check connectivity:
   ```bash
   ffprobe -rtsp_transport tcp \
     rtsp://admin:CHANGEME@41.90.110.206:554/cam/realmonitor?channel=1&subtype=0
   ```
3. In the VivoGuard UI: **Cameras → + Add camera → Dahua NVR
   (multi-channel)**. Fill in:
   - Host: `41.90.110.206`
   - Network: `WAN`
   - HTTP port: `80`, RTSP port: `554`, SDK port (optional): `7000`
   - Username / password
4. Click **Connect NVR**. The platform calls
   `/cgi-bin/magicBox.cgi?action=getSystemInfo` and
   `/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle` to
   enumerate channels, then probes channel 1's substream RTSP URL to
   confirm the path. Pick the channels you want and **Add channels** —
   each becomes its own row on the Cameras page.
5. AI inference starts automatically: the streamer service picks up the
   new cameras on its next reconcile (every 10s by default), opens an
   FFmpeg pipe to the substream (640×360 @ 5 FPS for bandwidth
   efficiency), and pushes frames into Redis. The Celery worker pulls
   frames, runs YOLOv8, applies the detection chain, and surfaces
   alerts on the dashboard.

---

# 4. Architectural notes

- **No proprietary SDKs**: this build deliberately uses only documented
  HTTP / RTSP / ISAPI / ONVIF interfaces. The Dahua NetSDK and Hikvision
  HCNetSDK ports (`7000`, `8000`) are accepted as connection-form fields
  for completeness but the runtime does not link against vendor C SDKs.
  The `vendor_sdk/` directory is gitignored and reserved for users who
  want to build a ctypes plugin themselves.
- **Single-tenant**: tables do not carry an `org_id` column. JWT auth
  with three roles (admin / operator / viewer) gates the API.
- **WAN-friendly streaming**: substreams (`subtype=1`) are used for the
  AI path; mainstream is reserved for live view + recording. Reconnect
  uses exponential backoff governed by `WAN_RECONNECT_*` envs.
- **DDNS**: the `maintenance.refresh_ddns` Celery task re-resolves DDNS
  hostnames on a configurable interval (`DDNS_REFRESH_INTERVAL_SECONDS`).
- **Continuous learning**: confirming an alert promotes the event's frame
  + bbox into the `feedback-<detection_type>` dataset; that dataset can
  be retrained on a schedule.

---

# 5. License & support

VivoGuard AI is provided as-is by the platform owner. See the license
file at the repository root if present, or contact the maintainer for
commercial deployment support.
