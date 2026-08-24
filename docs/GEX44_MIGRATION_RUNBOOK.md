# GEX44 production migration runbook

## Decision and constraints

The target is one Hetzner GEX44 in Falkenstein with Ubuntu 24.04 LTS,
primary IPv4, 64 GB RAM, two 1.92 TB NVMe drives and one NVIDIA RTX 4000
SFF Ada GPU. The complete stack moves together so PostgreSQL, Redis,
recordings and incident evidence remain on one trusted host.

Do not treat the GPU purchase as proof of coverage or 99% accuracy. The
current production loop uses long-lived per-camera Celery tasks. Controlled
distribution and the model-level batch primitive are available, but both stay
at their backward-compatible defaults until the actual GPU capacity test
passes. Buying hardware is not permission to guess a concurrency setting.

## Purchase checkpoint

Before placing the order, record the live monthly price, setup fee, VAT
treatment, location and stock status. Ordering is a financial commitment and
requires action-time approval. Do not substitute GEX131 or another server
without a separate cost and architecture decision.

## Prepare the host

1. Install Ubuntu 24.04 LTS and all security updates.
2. Create named administrator accounts, disable password SSH after verifying
   key access, enable the Hetzner firewall and retain console recovery.
3. Install Docker Engine from Docker's signed repository.
4. Install the supported NVIDIA driver and NVIDIA Container Toolkit. Configure
   Docker using `nvidia-ctk runtime configure --runtime=docker`, then restart
   Docker.
5. Clone the repository at the exact deployed commit and restore the production
   `.env` from the approved secret store. Never copy secrets into Git or shell
   history.
6. Run `bash scripts/gpu-readiness.sh`. Do not migrate data until every check
   passes.

## GPU batch and concurrency validation

Before copying production data, build a dynamic TensorRT profile and benchmark
it in an isolated container. This test uses synthetic frames and neither reads
camera feeds nor writes VivoGuard data:

```bash
INFERENCE_MAX_BATCH_SIZE=32 docker compose \
  -f docker-compose.yml -f docker-compose.gpu.yml build worker-inference
INFERENCE_MAX_BATCH_SIZE=32 docker compose \
  -f docker-compose.yml -f docker-compose.gpu.yml run --rm --no-deps \
  worker-inference python scripts/gpu_concurrency_benchmark.py \
  --batch-sizes 1,2,4,8,16,32 --iterations 50 \
  > gpu-capacity-baseline.json
```

The script fails when CUDA is unavailable and reports p50/p95 batch latency,
p95 latency per frame, throughput, peak allocated VRAM, failures and the
largest batch passing the initial 400 ms/frame and 80% VRAM ceilings. Retain
the JSON as deployment evidence. These are safety ceilings, not the final
service SLA; the two-hour live canary below must also pass end-to-end alert
latency and missed-event tests.

Do not equate batch size with Celery concurrency. The current per-camera path
remains the production rollback path. Start its GPU worker conservatively and
only increase worker concurrency while total VRAM stays below the measured
ceiling and p99 alert latency improves. Multiple worker processes duplicate
model/engine memory.

## Controlled distribution

The default remains one shard and the historical `inference` queue. For a
future multi-GPU or multi-host deployment, set the supervisor's
`INFERENCE_SHARD_COUNT=N`; camera `id` is deterministically assigned to
`inference.(id % N)`. Provision consumers for every queue before changing the
count, for example `INFERENCE_QUEUES=inference.0` on worker zero. Never expose
Redis directly to the internet: use a private network or authenticated tunnel,
and provide shared durable evidence storage before separating inference from
the application host. Queue depth is reported per shard in
`vg:inference:health`, so an absent consumer is visible immediately.

## Storage and backups

- Configure the two NVMe devices as RAID 1 unless an approved durability design
  provides equivalent protection. RAID is availability, not backup.
- Take an encrypted off-host PostgreSQL dump and an off-host copy of critical
  evidence before cutover. Verify both by restoring into an isolated database.
- Copy `data/recordings`, `data/models`, `data/datasets`, `data/thumbnails` and
  TLS material using an encrypted channel. Preserve ownership and timestamps.
- Keep the old server and its data unchanged for at least seven days after
  acceptance. Do not cancel it at cutover.

## Rehearsal

1. Restore a recent database dump on the GEX44 while the old production host
   remains authoritative.
2. Start the stack on a temporary private hostname. Do not send production
   notifications during rehearsal.
3. Build and start GPU inference with both Compose files:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.gpu.yml build worker-inference
   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
   ```

4. Confirm the log reports `backend=cuda`, the worker uses
   `vivoguard/worker:cuda`, and `nvidia-smi` shows bounded VRAM use.
5. Validate login, database migrations, Odoo signature rejection/acceptance,
   alert delivery in test mode, fresh frames, snapshots and incident clips.
6. Run a representative camera canary for at least two hours. Capture frame
   rate, p50/p95/p99 inference latency, alert latency, dropped frames, CPU,
   GPU, VRAM, queue depth and thermal state.

## Cutover

1. Lower DNS TTL at least one day beforehand where the DNS provider permits.
2. Announce a controlled maintenance window and pause application writes and
   workers on the old host. Camera/NVR recording continues independently.
3. Create the final PostgreSQL dump and final incremental evidence sync.
4. Restore on GEX44, run `alembic upgrade head`, then start the stack with the
   GPU override.
5. Issue/restore TLS certificates, update the DNS A record and verify public
   `/api/healthz` before allowing user traffic.
6. Resume alert delivery only after timestamps, camera IDs, notification
   recipients and clip retrieval pass the cutover checklist.

## Acceptance gates

- Public API and authenticated VivoOps flows are healthy.
- Every expected service is running the intended commit and migration head.
- CUDA is active; no inference worker silently falls back to CPU.
- All previously fresh cameras resume fresh frames and no working camera is
  lost relative to the pre-cutover baseline.
- Snapshot and incident-clip retrieval succeed for canary cameras.
- Alert p95 and p99 latency improve without duplicate or missed-event growth.
- CPU retains at least 20% sustained headroom and GPU/VRAM remain below the
  measured safe ceiling during busy periods.
- Off-host backup restore and rollback procedures have been exercised.

## Rollback

If any acceptance gate fails, stop writes and notifications on GEX44, point DNS
back to the unchanged old host, restore its workers, and reconcile only the
audited records created during the cutover window. Never run both hosts as
independent writable production systems against different databases.
