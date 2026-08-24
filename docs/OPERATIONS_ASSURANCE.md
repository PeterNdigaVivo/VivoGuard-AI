# VivoGuard operations assurance

This layer creates evidence-backed work for a human operator. It does not make
criminal, employment or disciplinary decisions.

## Capability and deployment states

Use these terms precisely:

- **Implemented**: code and migration exist in the repository.
- **Verified**: automated tests/build have passed for the commit.
- **Deployed**: the intended commit is running in the production API/workers
  and the repository's current Alembic head is applied.
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
If visual evidence is supplied it enters the quarantined training pool; a
bounding box and a second reviewer who is independent of both the reporter and
annotator are required before an object-detection annotation becomes eligible.
No synthetic or imagined evidence is created.

Alert verdicts also fail closed. A first review creates quarantined evidence. A
blind second review promotes it only on agreement. Disagreement creates a
`reviewer_disagreement` assurance case. A third operator, independent of both
earlier reviewers, must adjudicate that case with an evidence-based rationale.
The earlier decisions remain append-only. An `unclear` adjudication and any
camera-detector pair under quality control remain training-ineligible.

The scheduled alert-quality task checks acknowledgement SLA, event-to-alert
latency and missing evidence. It deduplicates cases by alert and issue set.

**Measure recall** creates reproducible random samples from retained recordings.
The extraction worker writes bounded 10–120 second clips into a dedicated
purpose-limited directory without exposing source recording paths. Reviewers are
blind to alert history. Two distinct reviewers must agree; disagreement requires
a third reviewer. Only then does VivoGuard check for a matching alert and record
an independently sampled true positive or false negative. Sample clips are
deleted seven days after review. They never become training data automatically.

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

1. Deploy the verified commit and apply the current Alembic head.
2. Map exact Odoo store IDs and event names in staging; use a service credential
   and signed webhook transport at the gateway.
3. Configure and approve critical zones camera by camera.
4. Fire synthetic delivery, void/refund/no-sale and missed-event cases.
5. Verify timestamp/timezone, idempotency, evidence retrieval and SLA timing.
6. Train operators on non-accusatory review, privacy and escalation.
7. Observe for seven days, then tune thresholds from false-positive and
   false-negative evidence.

## Independent-validation checkpoint

Software can enforce sampling, review separation, quarantine and auditability;
it cannot manufacture independent ground truth. The validation owner must:

1. Assign at least three distinct operator accounts: primary reviewer,
   independent reviewer and adjudicator.
2. Review real alert evidence in **AI Learning → Labelling Sprint**.
3. Use **Independent review** from a different account without seeing the first
   verdict.
4. Use **Resolve disagreements** from a third account when the first two differ.
5. Record real missed or late incidents with **Report missed alert** and attach
   retained visual evidence where lawful and available.
6. Use **Measure recall** to generate seeded random-footage batches. Review the
   footage before checking alert history; alert review alone cannot establish
   recall.

The acceptance gate is detector- and camera-specific: sufficient representative
sample size, 95% confidence bounds, reviewer agreement, clip availability and
measured recall must all pass. A low alert count, simulation pass rate or overall
fleet average is not evidence of 99% real-world performance.
