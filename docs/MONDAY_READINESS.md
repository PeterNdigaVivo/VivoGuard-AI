# VivoGuard Monday readiness gate

Target date: Monday, 24 August 2026 (Africa/Nairobi).

## What “99% true alerts” means

The target is **reviewed production precision**, measured separately by
detector, store, camera and operating condition. A simulator pass rate is not
precision. A credible 95% Wilson lower confidence bound above 99% requires at
least 384 reviewed alerts with zero false positives for the measured slice.
Always publish numerator, denominator, false positives and confidence bound.

Precision alone can hide a detector that suppresses everything. Report recall
from human-reported/missed-event cases separately, with zero unresolved missed
critical events as the Monday life-safety gate.

## Release-blocking gates

- Migration 0038 applied and training provenance verified.
- Synthetic, simulated, mined and ambiguous evidence has zero path into model
  training until explicit review; isolation tests pass.
- All isolated policy scenarios pass, with zero production alerts/messages and
  zero trainable samples created by the simulation run.
- Every active store has approved entrance, POS/cash, sales-floor and
  stockroom/back-door coverage requirements.
- Critical alerts: acknowledgement within five minutes, evidence attached and
  no unresolved delivery/deduplication breach.
- High alerts: acknowledgement within 30 minutes.
- Monitoring agents: at least 99% run coverage, completion reliability and
  valid evidence-backed output in the reported window. An active critical
  breach overrides any average score.
- Offline/shared-NVR faults and pending feeds are explicitly accepted by an
  accountable owner or restored; unavailable coverage cannot be described as
  monitored.
- Odoo store/event mapping, idempotency and pseudonymisation pass staging tests.
- Repository commit, deployed commit and applied migration are recorded as
  separate evidence.

## Human-feedback clarification

If a WhatsApp response is unclear, do not infer a label or resolution. Ask for
the missing store, camera/channel, incident time, observed behaviour, expected
system behaviour, and screenshot/clip where relevant. Use 15 minutes for
urgent, 30 minutes for high priority and two business hours for routine
clarifications. Send one reminder, then escalate the accountable owner. An
ambiguous or unanswered report remains blocked and cannot enter training.

## Simulation lanes

The current fast lane evaluates isolated policy scenarios hourly. It covers
static-object suppression, real-person pause protection, camera/clip failures,
alert SLAs, after-hours lone working, POS review, delivery windows, stale
frames, duplicate floods and ambiguous feedback.

The next lane must replay approved, de-identified held-out clips offline for
weapon, fight, fall, fire/smoke, intrusion, queue and counter-boundary cases.
Replay media remains evaluation-only with hashes and
`eligible_for_training=false` unless independently promoted through review.

## Accountability

The scorecard names an owner and exposes expected/completed runs, run coverage,
completion reliability, valid-output rate, current freshness and active
critical override. Current limitations are displayed rather than hidden:
durable scheduled/start timestamps are still needed for exact queue-delay
measurement, and WhatsApp delivery cannot be counted until a provider returns
message delivery receipts.
