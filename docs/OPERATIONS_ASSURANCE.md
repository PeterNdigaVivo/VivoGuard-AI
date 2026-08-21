# VivoGuard operations assurance

This layer creates evidence-backed work for a human operator. It does not make
criminal, employment or disciplinary decisions.

## Capability and deployment states

Use these terms precisely:

- **Implemented**: code and migration exist in the repository.
- **Verified**: automated tests/build have passed for the commit.
- **Deployed**: the commit is running in the production API/workers and migration 0037 is applied.
- **Configured**: every active store has approved critical-zone mappings and source-system mappings.
- **Operationally validated**: live test events, clips, SLAs and operator workflows have passed per store.

No capability is production-ready merely because it is implemented.

## Minimum critical-zone map

Each store should explicitly map entrance/exit, POS/cash counter, diagonal
sales-floor/high-value merchandise, and stockroom/back door. Higher-risk sites
should also map delivery access and window/shutter coverage. Each requirement
sets a maximum frame age, required camera count and incident-clip requirement.
Missing configuration is itself a critical assurance case.

## Missed events and alert quality

`POST /operations/missed-events` records a human report, searches for nearby
detections, assigns a root-cause category and creates a labelled assurance case.
If visual evidence is supplied it enters the training pool; a bounding box is
required before an object-detection annotation is verified. No synthetic or
imagined evidence is created.

The scheduled alert-quality task checks acknowledgement SLA, event-to-alert
latency and missing evidence. It deduplicates cases by alert and issue set.

## Lone worker and late departure

The scheduled task creates a human-review case only when one tracked person is
seen in a closed store during the lookback window. The wording requires welfare
and authorisation checks and does not infer misconduct. Sites without reliable
tracking should not use this result as proof that a person was alone.

## Delivery, inventory and Odoo POS

`POST /operations/events` accepts an idempotent minimal event contract for POS,
inventory and delivery events. `app.integrations.odoo_pos` maps supported Odoo
webhooks to that contract. Employee references are pseudonymised and common
direct identifiers are removed from payloads before storage.

Correlation uses a five-minute CCTV evidence window and produces a transparent
review score. Missing camera evidence increases operational urgency because it
is a control gap; it is never treated as evidence against a person. All reviews
remain `pending_human_review` until an authorised operator records a conclusion.

Resolved assurance cases and reviewed operational events are retained for 365
days; governance audit records are retained for 730 days. Open/unreviewed work
is not removed by the scheduled retention task.

## Production activation checklist

1. Deploy the verified commit and apply Alembic migration 0037.
2. Map exact Odoo store IDs and event names in staging; use a service credential
   and signed webhook transport at the gateway.
3. Configure and approve critical zones camera by camera.
4. Fire synthetic delivery, void/refund/no-sale and missed-event cases.
5. Verify timestamp/timezone, idempotency, evidence retrieval and SLA timing.
6. Train operators on non-accusatory review, privacy and escalation.
7. Observe for seven days, then tune thresholds from false-positive and
   false-negative evidence.
