# GPU inference slot budget

How many cameras can run AI at once, what pinning costs, and when a
second/bigger GPU is needed.

## Where the numbers come from

- **Slots** = `WORKER_INFERENCE_CONCURRENCY` (compose default 8; the
  production GPU server overrides it in `.env` — check with
  `grep WORKER_INFERENCE_CONCURRENCY .env`; the observed fleet behaviour
  of ~28 concurrent implies it is set to 28 there).
- Every `ai_enabled` camera with fresh frames is enqueued each
  supervisor tick; cameras beyond the slot count wait in the queue —
  that queueing IS the "rotation". Nothing else rotates cameras.
- A camera shows **AI-active** on System Health when `ai_enabled` is on
  AND its inference heartbeat is fresher than 60 s — i.e. a slice is
  actually running. Model deployment is NOT part of this: cameras run
  the base model when no custom model is deployed.

## Scheduling tiers (highest first)

| Tier | Which cameras | Slice | Queue priority |
|---|---|---|---|
| Critical | intrusion/fire/entry_exit-type configs or zones | `INFERENCE_CRITICAL_SLICE_SECONDS` (15 s) | 0 |
| **Pinned** | `always_on_ai` flag (📌 on the Cameras page) | `INFERENCE_PINNED_SLICE_SECONDS` (3600 s) | 1 |
| Rotating | everyone else | `INFERENCE_RUN_SECONDS` (120 s) | 9 |

A pinned camera's hour-long slice holds its worker slot; when it ends,
the camera re-queues ahead of the rotating fleet, so its worst-case gap
is one supervisor tick (~30 s) when a slot frees — in practice it stays
continuously AI-active. **One pinned camera permanently consumes one
slot.**

## The budget at 28 slots (current GPU)

| | Slots |
|---|---|
| Total | 28 |
| Pinned entrance cameras (1/store, ~26 stores) | −26 |
| **Left for everything else** | **2** |

Two rotating slots across ~100 remaining AI-enabled cameras at 120 s
slices = each non-pinned camera gets a slice roughly every **100 min**,
and critical-tier cameras will constantly preempt those 2 slots. That
is not workable as a steady state.

### Realistic options

1. **Pin fewer stores first.** Pin the ~10 highest-priority stores
   (−10 slots → 18 rotating slots → non-pinned cameras cycle every
   ~10–12 min, similar to today). Expand as capacity grows.
2. **Shorten pinned slices** (`INFERENCE_PINNED_SLICE_SECONDS=1800`)
   — changes nothing about slot math, only crash-recovery granularity.
3. **Second / bigger GPU** for all-26 pinning plus a healthy rotation:
   budget ≈ 26 pinned + 1 rotating slot per ~6 rotating cameras for a
   ≤12-min revisit. For ~100 rotating cameras that is ~17 rotating
   slots → **~43 total slots**, i.e. roughly 1.5× the current 28-slot
   GPU. Verify by raising `WORKER_INFERENCE_CONCURRENCY` gradually and
   watching GPU memory/util plus per-slice FPS on Mission Control.

## Operating notes

- Pin via the 📌 button on the Cameras page (admin/operator), or
  `PATCH /api/cameras/{id}` with `{"always_on_ai": true}`.
- Pinning does not bypass the frame-freshness gate: an offline camera
  is never scheduled, pinned or not.
- Latency-critical cameras need no pinning — their tier already outranks
  pinned and they keep short slices by design (pinning a critical camera
  changes nothing; the critical tier wins).
