# Deployment guide

VivoGuard AI ships three deployment shapes. Pick the one that matches your scale.

| Profile           | Footprint     | When to use                                      |
|-------------------|---------------|--------------------------------------------------|
| **Edge appliance**| ~1.7 GB disk, 2 GB RAM, 2 containers | One site, ≤ 8 cameras, no DBA, no infra team |
| **Single-server (compose)** | ~3 GB disk, 8–16 GB RAM, 7–8 containers | Single physical box, 8–32 cameras |
| **Kubernetes (Helm)**       | per-node footprint depends on placement; HA   | Multi-node, autoscaling, 32+ cameras, business-critical |

## Image size summary

The new split images keep heavy AI deps out of services that don't need them.

| Image                   | Approx. size | Layers used by                |
|-------------------------|--------------|-------------------------------|
| `vivoguard/api`         | **~300 MB**  | api, streamer (FROM api)      |
| `vivoguard/worker` (CPU)| ~1.5 GB      | worker                        |
| `vivoguard/worker` (GPU)| ~5 GB        | worker (CUDA wheels)          |
| `vivoguard/frontend`    | ~50 MB       | frontend (nginx + dist)       |
| `postgres:16-alpine`    | ~250 MB      | postgres                      |
| `redis:7-alpine`        | ~40 MB       | redis                         |
| `minio:latest`          | ~250 MB      | minio (optional)              |
| `nginx:alpine`          | ~50 MB       | nginx                         |

**Total previous (single fat image)**: ~6.5 GB across api+worker+streamer.
**Total now (split + CPU torch)**: api ~300 MB shared between api+streamer, worker ~1.5 GB. **Roughly 4 GB saved on a typical single-node deploy.**

---

## 1. Edge appliance — fastest path

Two containers, SQLite, filesystem object storage. No Postgres, no Redis, no MinIO.

```bash
git clone -b claude/vivoguard-ai-platform-YE5iF https://github.com/PeterNdigaVivo/VivoGuard-AI.git
cd VivoGuard-AI

# Generate strong secrets and a base .env (the deploy script does this for you)
./scripts/deploy.sh                       # writes .env, exits — edit it now
nano .env                                 # set BOOTSTRAP_ADMIN_*, USE_GPU=false

# Bring up the edge profile (overlays the main compose):
docker compose \
  -f docker-compose.yml \
  -f docker-compose.edge.yml \
  up -d --build
```

**Tradeoffs:**

- SQLite is fine for ≤ 8 cameras and ≤ 100k detections; the alert query slows beyond that.
- No Redis ⇒ no live-frame WebSocket fan-out across multiple browser sessions. Snapshots still work.
- No background Celery worker — AI runs eagerly inside the API process. CPU-bound at ~5 FPS × 4 cameras.

Migrate to the single-server profile (below) when you outgrow this.

---

## 2. Single-server with Docker Compose

Default deploy. Postgres + Redis in-cluster, MinIO is opt-in.

### 2.1 Standard install

```bash
./scripts/deploy.sh                       # mints .env on first run
nano .env                                 # set BOOTSTRAP_ADMIN_*, USE_GPU
./scripts/deploy.sh                       # builds images, brings up the stack
```

Brings up: **api · worker · streamer · frontend · postgres · redis · nginx** (7 containers, ~3 GB images, ~1 GB RAM idle).

### 2.2 With MinIO (S3-compatible local store)

If you need an S3 API for alert clip retention, recordings export, or
offsite backups, add the `object-store` profile:

```bash
docker compose --profile object-store up -d
```

### 2.3 With managed Postgres / Redis / S3 (cloud overlay)

Drops the in-cluster stateful services entirely.

```bash
# .env: point at the managed services
POSTGRES_HOST=mydb.cluster-xyz.us-east-1.rds.amazonaws.com
POSTGRES_PASSWORD=<strong>
REDIS_HOST=mycluster.xxx.cache.amazonaws.com
S3_ENDPOINT=https://s3.us-east-1.amazonaws.com
S3_ACCESS_KEY=AKIA...
S3_SECRET_KEY=...
S3_USE_SSL=true

docker compose \
  -f docker-compose.yml \
  -f docker-compose.cloud.yml \
  up -d
```

### 2.4 GPU inference

```bash
docker compose build --build-arg GPU=true worker     # ~5 GB CUDA image
# Uncomment the `<<: *gpu-reservation` line under `worker:` in docker-compose.yml
docker compose up -d worker
```

### 2.5 Scaling on a single host

```bash
docker compose up -d --scale api=3 --scale worker=4
```

The api/worker services no longer have fixed `container_name`s, so this works out of the box. Nginx already round-robins via the `vivoguard_api` upstream.

---

## 3. Kubernetes (Helm) — HA + autoscaling

Use this for production. Multiple api replicas behind a Service, HorizontalPodAutoscaler on api and worker, optional GPU node selector, optional managed datastores.

### 3.1 Push the images

Replace `ghcr.io/peterndigavivo` with your registry.

```bash
docker compose build api worker streamer frontend
for i in api worker streamer frontend; do
  docker tag vivoguard/$i:latest ghcr.io/peterndigavivo/vivoguard/$i:latest
  docker push           ghcr.io/peterndigavivo/vivoguard/$i:latest
done
```

### 3.2 Install — bundled stateful services

```bash
helm install vg deploy/k8s \
     --namespace vivoguard --create-namespace \
     --set image.registry=ghcr.io/peterndigavivo \
     --set ingress.host=vivoguard.example.com \
     --set ingress.tls.enabled=true \
     --set secrets.jwtSecret="$(openssl rand -hex 48)" \
     --set secrets.fernetKey="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
     --set secrets.bootstrapAdminPw="$(openssl rand -hex 16)"
```

This creates:

- `Deployment vivoguard-api` × 2 replicas (HPA → 10)
- `Deployment vivoguard-worker` × 2 replicas (HPA → 8)
- `Deployment vivoguard-streamer` × 1
- `Deployment vivoguard-frontend` × 2
- `StatefulSet vivoguard-postgres` × 1 (PVC 50 GiB)
- `StatefulSet vivoguard-redis` × 1 (PVC 5 GiB)
- `StatefulSet vivoguard-minio` × 1 (PVC 200 GiB)
- 4× shared `ReadWriteMany` PVCs for models / datasets / thumbnails / recordings
- Ingress on `vivoguard.example.com`

### 3.3 Install — managed Postgres / Redis / S3

```bash
helm install vg deploy/k8s \
     --namespace vivoguard --create-namespace \
     --set postgres.enabled=false \
     --set redis.enabled=false \
     --set minio.enabled=false \
     --set externalDatabase.url='postgresql+psycopg://user:pw@rds-host:5432/vg' \
     --set externalRedis.url='redis://elasticache-host:6379/0' \
     --set externalS3.endpoint='https://s3.us-east-1.amazonaws.com' \
     --set externalS3.accessKey='AKIA...' \
     --set externalS3.secretKey='...' \
     --set externalS3.useSSL=true \
     ...
```

### 3.4 GPU workers

```bash
helm upgrade vg deploy/k8s \
     --reuse-values \
     --set worker.gpu.enabled=true \
     --set image.worker.tag=gpu \
     --set worker.resources.limits."nvidia\.com/gpu"=1
```

Requires the [NVIDIA k8s device plugin](https://github.com/NVIDIA/k8s-device-plugin) and a GPU node pool labelled `nvidia.com/gpu.present=true`.

### 3.5 Availability characteristics

- API tier: **N+1 redundancy** (≥ 2 replicas behind a ClusterIP service; survives single-node failures).
- Worker tier: scales 1 → 8 on CPU (or queue length, with a custom KEDA scaler if you adopt it later).
- Postgres / Redis: single-replica StatefulSets in the bundled chart. For real HA, point at managed services (3.3) or replace the chart's StatefulSet with the [Bitnami postgresql-ha](https://github.com/bitnami/charts/tree/main/bitnami/postgresql-ha) and [bitnami/redis](https://github.com/bitnami/charts/tree/main/bitnami/redis) charts as subcharts.
- MinIO: replace with managed S3 for cross-AZ durability.

### 3.6 Smoke test

```bash
kubectl -n vivoguard get pods
kubectl -n vivoguard logs deploy/vivoguard-api    | tail -20
kubectl -n vivoguard exec deploy/vivoguard-api -- alembic current
kubectl -n vivoguard port-forward svc/vivoguard-api 8000:8000   # then curl localhost:8000/healthz
```

---

## Choosing between methods

```
                    +--------------------------+
                    |   How many cameras?      |
                    +-----------+--------------+
                                |
              ≤ 8                 9 – 31              ≥ 32
              |                     |                   |
              v                     v                   v
       Edge appliance       Single-server (compose)   Kubernetes (Helm)
       (sqlite, fs)         (postgres + redis)        (HPA, multi-AZ)
       2 containers         7–8 containers            ≥ 10 pods
```

If you're not sure: **start with single-server**, run for a week to learn your camera count and FPS budget, then re-evaluate. Migrating between profiles is a `pg_dump` and `pg_restore` away because the schema is identical across all three.
