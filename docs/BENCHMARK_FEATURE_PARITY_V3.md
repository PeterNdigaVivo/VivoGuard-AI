# VivoGuard AI — benchmark and feature-parity build review (v3)

Evidence cut: 21 August 2026, 21:10 EAT. This is a design and benchmark artefact, not a deployment approval.

## Executive decision

VivoGuard does not currently pass either the release gate or the governance gate. The three highest-value changes are:

1. Restore camera availability and make alert/evidence delivery durable before adding a VLM. At the evidence cut, only 55/101 active endpoints were live; 43 were offline and 3 stale.
2. Establish an independently sampled ground-truth programme and immutable incident/review links. Today's reviewed-alert precision is only an indicative 31.97% (39/122; 95% Wilson CI 24.35–40.68%); recall is unknown.
3. Put a versioned incident state machine and transactional outbox between detection persistence and external delivery. Redis pub/sub and mutable `:latest` images cannot support delivery guarantees or safe rollback.

A VLM on the current host is blocked. The 16-vCPU VM has no GPU; inference and streaming were consuming roughly 8.8 and 6.8 CPU cores respectively at the sample, and containers have host-wide memory limits rather than enforced service limits. A verifier belongs on a separately capacity-controlled host or managed endpoint after shadow evaluation.

## Pass A — benchmark

### A.0 Verified current system

#### Fleet and host

| Claim | Verified finding | Confidence |
|---|---|---|
| Stores | 28 active database stores across Kenya, Uganda and Rwanda | High: production DB |
| Camera count | 101 active camera rows and 101 distinct configured endpoint keys. This does **not** prove 101 unique physical cameras; field inventory is still required. | High for rows/endpoints; low for physical devices |
| Live coverage | 55 online, 43 offline, 3 stale at 21:10 EAT. Database labels separately showed 28 online, 19 offline and 54 pending, proving the stored label is not live truth. | High: live System view + DB |
| Compute | Hetzner KVM vServer, Ubuntu 22.04, 16 AMD EPYC vCPUs, 30 GiB RAM, 601 GB disk, no NVIDIA device/runtime | High: host probe |
| Runtime | Docker Engine 29.5.3, Compose 5.1.4; migration 0039 at head | High: host probe |
| Deployment | Single host, source build through Compose, mutable `vivoguard/*:latest` tags; several running images displayed only by SHA | High |
| Storage | 103.4/600.6 GB used in VivoOps; host filesystem 99/601 GB used in an earlier probe | High |

#### Inference and temporal data

- Core path is JPEG frame buffer → YOLO through Ultralytics/ONNX-capable wrapper → per-camera Supervision ByteTrack → detector registry → `DetectionEvent`/`Alert` persistence → Redis publication.
- ByteTrack is per-camera and in-process. IDs reset on worker/stream restart. Track history is bounded to 64 observations.
- A static-person filter uses movement history; fixed-scene incident fingerprints add restart-safe intrusion deduplication.
- The only broadly consumable live track feed is `vg:dets:{camera_id}`: sampled to 1 Hz, capped at 25 persons, 15-second TTL, and Redis-local. It is not durable, ordered, replayable, or externally contracted.
- `DetectionEvent` records threshold-crossing events, not every track observation. Therefore the alert table cannot reconstruct arbitrary dwell, loitering, crowd or cross-camera histories.
- Required additive boundary: emit durable **track-transition events**, not every frame. Source identity must be created at the tracking/persistence boundary using `(camera_id, stream_epoch, tracker_id, transition_seq)`; downstream services must not invent identity.

#### Scenarios already present

The repository already contains detectors or workflows for intrusion, entry/exit, crowd, checkout dwell, staff/restricted zones, store open/close, queue, shelf/shrinkage signals, stockroom, shutters, uniform compliance, system health, camera coverage, alert quality, lone-worker review, Odoo operational-event intake, simulation, training and agent accountability. Several perception-dependent classes are stubs or require custom models; presence in the catalogue is not proof of production performance.

#### Alerting and evidence

- Automatic WhatsApp delivery is intentionally disabled in both the notifier and briefing helper. Current group messages are manual browser communications. That is not a reliable notification channel.
- The alert engine consumes ephemeral Redis pub/sub, deduplicates in process for 5/30 seconds, and fans out best-effort. There is no durable delivery outbox, provider receipt state or replay after subscriber downtime.
- Today: 331 raw alerts; 39 confirmed, 83 dismissed, 209 new. Only 12 alerts have append-only `AlertReviewDecision` rows because that mechanism was deployed during the day.
- Today: 118/331 (35.65%) have an `extra.alert_clip_path`; 294/331 (88.82%) have a thumbnail, and 63 have a filmstrip. `DetectionEvent.clip_path` itself was empty for all 331, so clients must know two clip conventions.
- Seven-day acknowledgement latency among 347 acknowledged alerts: p50 9,648 s, p95 44,058 s, p99 52,434 s. This is human acknowledgement latency, not system or delivery latency.
- The production UI and DB disagree on same-day alert totals (for example 312 vs 331), so raw, grouped, positive and hidden alert semantics need a common metric definition.

#### Store schedules

All 28 active stores contain the same legacy schedule shape:

```json
{"monday":{"open":"09:30","close":"20:00"}, "tuesday":{"open":"09:30","close":"20:00"}, "...":"..."}
```

Current code expects abbreviated keys and arrays, for example `{"mon":["09:30-20:00"]}`. The UI consequently displays stores such as Junction and Eldoret as closed all week, while some intrusion paths fall back to 09:00–21:00. Holiday overrides are not represented. Schedule-driven alerts are therefore unfit for fleet rollout until migrated and validated.

#### Measured performance

- CPU/ONNX chain average inference: 1,973.78 ms over 58 reporting cameras and 320,441 frames in the preceding 24 hours.
- Slow examples: Acacia Ch3 p99 19.21 s; Mama Ngina Ch1 p99 16.45 s; Runda Ch1 p99 16.30 s.
- Many live streams process at 0.5–1.3 FPS; short events can be missed before any verifier is invoked.
- Current containers have no effective per-service CPU/memory ceilings: `docker stats` shows each service limited to the full 30.59 GiB host.

### A.0 Measurement protocol

#### Metric definitions

| Metric | Definition |
|---|---|
| Alert precision | distinct adjudicated true incidents / distinct adjudicated alerted incidents; duplicates excluded from denominator |
| Event recall | independently discovered true incidents that generated a correct alert / all independently discovered true incidents |
| False alerts/camera-day | adjudicated false distinct incidents / eligible online camera-days; report by detector and day/night |
| Alerts/store-day | distinct incidents / store-days, with raw alert count shown separately |
| Duplicate rate | duplicate alerts beyond the first / all alerts belonging to adjudicated incidents |
| System latency | persisted alert time minus first processed qualifying frame time |
| Detection delay | persisted alert time minus human-annotated event onset; only staged/retrospective data can measure this |
| Delivery latency | provider accepted/received time minus outbox-created time; browser posting does not qualify |
| Evidence integrity | playable, correct camera/time/incident clip among clip-eligible alerts |
| SLA | p50/p95/p99 time to acknowledge and resolve, by severity and operating period |

Today's dismissed-alert density is 83/101 = 0.82 per configured camera-day and raw alert density is 3.28/camera-day. Both are partial-day, availability-confounded figures and must not be presented as a stable baseline.

#### Sampling plan

1. Freeze a 14-day held-out period after schedule/camera restoration. Do not tune on it.
2. Eligibility requires a fresh stream, correct clock, configured zone and retrievable evidence. Track excluded camera-hours explicitly.
3. Stratify by detector, store, camera, open/closed, day/night, image quality and FPS band. Randomly select within strata; review all alerts in sparse strata.
4. Minimum initial precision sample: 100 reviewed distinct incidents per material detector and at least 30 eligible camera-days across at least five stores. This estimates performance; it does not prove 99%.
5. Double-review at least 20% and at least 50 incidents per material detector. Use four outcomes: true, false, misclassified, unclear; classify duplicates separately. Require Cohen's kappa ≥0.70, otherwise adjudicate and retrain reviewers.
6. Independently sample footage windows for missed events. Human reports and staged events are additional recall sources, not substitutes for random negative-window review.
7. Report Wilson/Clopper–Pearson intervals. To claim precision above 99% with a two-sided 95% lower bound and zero false alerts requires about 368 independent alerts; any false result requires a much larger sample. Never claim “100%” from the synthetic catalogue.
8. Keep canary-tuning data, model-training data and held-out evaluation data mutually exclusive by hash and incident family.

### A.1 Gate scorecard

Scores follow the supplied 0–5 anchors. `NV` means not verified. Vendor capability evidence is documentary, not a controlled retail trial; therefore competitor detection-quality cells are capped at 2.

Sources: [Ambient.ai platform](https://www.ambient.ai/), [Ambient integrations/privacy](https://www.ambient.ai/ai-info), [Coram platform](https://www.coram.ai/), [Coram May 2026 notes](https://help.coram.ai/en/articles/15346332-product-release-notes-may-2026), [Coram July 2026 RBAC notes](https://help.coram.ai/en/articles/16190127-product-release-notes-july-2026), [Verkada cameras](https://www.verkada.com/security-cameras/), [Verkada audit logs](https://help.verkada.com/command/organization-wide-settings/manage-and-view-audit-logs), [Verkada alert API](https://apidocs.verkada.com/reference/getnotificationsviewv1), [Lumana platform](https://www.lumana.ai/), [Lumana trust centre](https://www.lumana.ai/trust?data-id=application-security). All vendor material is vendor-authored and current as retrieved on 21 August 2026; confidence is Medium for capability existence and Low for performance.

#### Group 1 — release gate

| Dimension | VivoGuard | Ambient.ai | Coram | Verkada | Lumana |
|---|---:|---:|---:|---:|---:|
| Accuracy | 1 H | 2 L | 2 L | 2 L | 2 L |
| False-positive suppression | 1 H | 2 L (vendor says 95%+) | 2 L | 2 L | 2 L |
| Poor light/occlusion/low FPS | 1 H | NV | NV | NV | NV |
| Detection/system/delivery latency | 1 H | NV | NV | NV | NV |
| Availability | 1 H | NV | NV | NV | NV |
| Camera/NVR health | 2 H | 2 M | 2 M | 2 M | 2 M |
| Delivery guarantees | 0 H | NV | 2 M (webhooks documented) | 2 M (events API) | NV |
| Evidence/clip integrity | 1 H | 2 M | 2 M | 2 M | 2 M |

Result: VivoGuard fails. Every competitor is unproven without a controlled trial; none can be declared to pass.

#### Group 2 — governance gate

| Dimension | VivoGuard | Ambient.ai | Coram | Verkada | Lumana |
|---|---:|---:|---:|---:|---:|
| Triage/prioritisation | 2 H | 3 M | 3 M | 3 M | 3 M |
| Workflow and SLA | 2 H | 3 M | 3 M | 3 M | 3 M |
| RBAC and audit | 2 H | 3 M | 3 M | 4 M | 3 M |
| Privacy/retention/cross-border | 1 H | 3 M | 3 M | 3 M | 2 M |
| Cybersecurity/secrets | 1 H | 3 M | 3 M | 3 M | 3 M |
| Model/data provenance | 2 H | NV | NV | NV | NV |

Result: VivoGuard fails. Competitors' provenance and deployment-specific privacy remain contract/trial questions.

#### Groups 3–4 — differentiators

| Dimension | VivoGuard | Ambient.ai | Coram | Verkada | Lumana |
|---|---:|---:|---:|---:|---:|
| Scenario breadth | 2 H | 4 M | 4 M | 3 M | 3 M |
| Custom scenario definition | 1 H | 3 M | 4 M | 3 M | 3 M |
| Natural-language search | 0 H | 3 M | 4 M | 3 M | 3 M |
| Retail analytics | 2 H | 2 M | 2 M | 4 M | 2 L |
| Integration/API/POS | 1 H | 3 M | 3 M | 4 M | 2 L |
| Cost fit at 28 stores | 4 M | 1 L | 2 L | 1 L | 2 L |
| Camera agnosticism | 3 H | 4 M | 4 M | 2 M | 4 M |
| Sovereignty/self-hosting | 3 H | 2 M | 2 M | 1 M | 2 M |
| Maintainability/upgrade safety | 1 H | 3 L | 3 L | 4 M | 3 L |

No weighted total is reported because failed gates dominate differentiators.

### A.2 Gaps and priorities

| Gap | Bar | Minimum safe close | Risk if done badly |
|---|---|---|---|
| 46 unusable feeds | All vendors | ≥95% eligible critical-zone camera-hours; per-NVR ownership | False confidence and missed incidents |
| Unknown recall | Basic assurance | Independent missed-event sampling and staged tests | Optimising precision by suppressing real events |
| Ephemeral delivery | Coram/Verkada APIs | Transactional outbox, idempotency, receipts, replay | Silent alert loss or floods |
| Clip split/low coverage | Enterprise VMS | One canonical evidence manifest; ≥99% of clip-eligible alerts | Wrong-person/camera decisions |
| Invalid schedules | Coram timezone controls | Migrate, holiday overrides, store-manager sign-off | Fleet-wide false after-hours alerts |
| Slow/low-FPS processing | Release gate | ≥2 FPS general, ≥5 FPS entrance/POS; p95 inference target by class | Missed short events |
| Weak rollback | Mature SaaS | Digest pins, compatible migrations, backup/restore drill | Extended outage after release |
| Partial audit/privacy | Verkada audit | View/export/config audit, DPIA, verified deletion, cross-border record | Regulatory and insider-access risk |
| No durable track transitions | Search/rules peers | Bounded transition stream with source idempotency | Impossible temporal correctness or DB explosion |
| No search | Coram/Verkada | RBAC-filtered metadata search first | Privacy leakage across stores |

#### Keep / don't chase

Keep: existing cameras/NVRs, deterministic temporal rules, per-camera calibration, self-hosted economics, incident clips, Odoo event fusion, human review, operational agents and simulated regression tests.

Do not chase now: facial recognition, cross-camera biometric Re-ID, generative “understand anything” alerts, one service per scenario, unlimited frame-level logging, proprietary-camera replacement, or semantic video embeddings before structured metadata search works.

#### Ordered backlog

1. Feed/schedule/clock restoration and evidence eligibility.
2. Transactional outbox, receipt instrumentation and canonical evidence manifest.
3. Ground-truth sampling, immutable reviews and recall programme.
4. Incident state machine, stable source identities and transition stream.
5. Deterministic noise controls in shadow/canary.
6. Remote verifier shadow evaluation only.
7. Structured search, then optional NL translation.

## Pass B — additive build design

### B.0 Boundary and failure posture

The inference loop is frame acquisition, YOLO and ByteTrack. The alert-decision path starts with detector evaluation and includes persistence, deterministic gates, verifier policy, incident aggregation and delivery. A verifier that suppresses or delays an alert is inside the alert-decision path even if separately deployed.

“Fail open” means exact baseline semantics, not merely “an alert eventually exists.” When flags are off or the verifier is unavailable, contract tests must preserve: event/alert IDs, type, severity, camera/zone, snapshot/clip manifest, destination, incident ordering/dedup, training eligibility/review state, and ≤100 ms adapter overhead at p99. Current best-effort pub/sub behaviour is the baseline only until the outbox replaces it.

### B.0.1 Alert state machine

States are stored in a new `incident_state` projection; existing `Alert.status` remains compatible during migration. Every transition creates an append-only row.

| From | To | Actor | Preconditions |
|---|---|---|---|
| — | provisional | persistence boundary | valid source event + evidence manifest |
| provisional | verified | deterministic policy, validated verifier policy, or reviewer | reason/evidence; high consequence requires human or approved multi-signal policy |
| provisional | downgraded | policy/reviewer | severity change recorded; never for non-suppressible class |
| provisional | retracted | policy/reviewer | duplicate, invalid evidence or policy reason; not automatically “false” |
| provisional | expired | state service | class deadline elapsed without actionable confirmation |
| downgraded | verified/retracted/expired | reviewer/state service | append-only reason |
| any nonterminal | acknowledged | authorised operator | delivery/console identity recorded |
| acknowledged | resolved | authorised operator | resolution classification and note |
| resolved | verified/downgraded/retracted | reviewer/adjudicator | late verdict recorded without erasing resolution history |

Rules:

- Duplicates join one `incident_id`; each source alert remains traceable.
- Retraction stops the response SLA only when policy classifies it non-actionable; it never rewrites prior breach history.
- Late verdicts append a transition and, when an external provisional notification was delivered, enqueue a correlated follow-up.
- Provider message IDs and delivery receipts attach to `delivery_attempt`, not the alert row.
- Provisional/retracted/unclear alerts are training-ineligible. Only adjudicated labels enter training.
- WhatsApp messages cannot be recalled. The follow-up states “update/correction,” never pretends the original vanished.

Delivery semantics:

| Class | Semantics |
|---|---|
| Fire, weapon, violence, fall, forced entry | Immediate provisional delivery; never suppressed; async context only |
| After-hours/restricted intrusion | Deterministic gate ≤500 ms; immediate provisional if uncertain; multi-signal downgrade only after pair validation |
| Crowd, checkout, merchandising | Bounded hold ≤2 s for deterministic temporal gate; VLM remains async enrichment initially |
| System health | Immediate once per outage, recovery notice, bounded digest |
| Positive/authorised | Console/daily summary by default; clearly labelled positive; no urgent channel |

### B.1 Additive architecture and topology

```text
NVR/RTSP -> streamer -> frame buffer -> YOLO -> ByteTrack -> detectors
                                              | untouched inference loop
                                              v
                              persistence-boundary adapter
                              | event + outbox in one DB transaction
                              v
                    incident/state service -> policy service
                       |                 |         |
                       |                 |         +-> remote verifier (shadow)
                       |                 +-> evidence/clip worker
                       +-> delivery outbox -> provider -> receipt callback
```

Proposed production Compose profile after Phase 0 capacity tests:

```yaml
services:
  incident-state:
    image: registry/vivoguard/decision@sha256:<digest>
    cpus: "1.0"
    mem_limit: 768m
    pids_limit: 128
    read_only: true
  outbox-dispatcher:
    image: registry/vivoguard/decision@sha256:<digest>
    cpus: "0.5"
    mem_limit: 512m
    pids_limit: 128
  verifier-client:
    image: registry/vivoguard/verifier-client@sha256:<digest>
    cpus: "0.5"
    mem_limit: 512m
    pids_limit: 96
    # Network call only. No VLM weights on this host.
```

Do not apply these blindly: current inference + streamer CPU use leaves no reliable verifier headroom. First reserve four control-plane cores or move streaming/inference to another compute pool and demonstrate no FPS regression. Verify limits with `docker inspect` and an intentional OOM/burst test, not only `docker compose config`.

Phase-2 inference options:

- ≤400 ms synchronous target is plausible only for deterministic features or a task-specific small classifier on controlled hardware. It is not credible for Qwen2.5-VL-3B on this CPU VM.
- ≤3 s CPU is a measurement target, not an architecture guarantee. It may be achievable for Moondream-class/quantised small models with one warm image and tiny output, but multi-frame/context inputs and burst queueing are likely to exceed it.
- Qwen2.5-VL-3B is approximately three billion parameters and its official card supplies quality benchmarks, not this host's latency. Moondream's card supplies serving instructions, not a fleet p99 guarantee. Benchmark end-to-end decode, transfer, vision encode, generation, cold start and queue delay.
- Hetzner GEX configurations are fixed hardware; the current vServer cannot receive an in-place GPU. A GPU means a separate/migrated server. See [Hetzner GEX documentation](https://docs.hetzner.com/robot/dedicated-server/server-lines/gpu-server/).

### Data model, volume and retention

Additive tables:

- `source_event(event_uuid, camera_id, stream_epoch, source_seq, occurred_at, contract_version, payload_hash)`
- `track_transition(source_event_uuid, tracker_id, kind, zone_id, bbox_summary, occurred_at)`
- `incident(id, incident_key, class, camera_id, opened_at, evaluation_state, acknowledged_at, resolved_at, version)`
- `incident_member(incident_id, alert_id, source_event_uuid)`
- `incident_transition(incident_id, from_state, to_state, actor_type/id, reason_code, evidence, created_at)`
- `evidence_manifest(alert_id, snapshot_uri/hash, clip_uri/hash, starts_at, ends_at, eligibility_reason)`
- `delivery_outbox(idempotency_key, incident_id, channel, destination_ref, payload_version, available_at)`
- `delivery_attempt(outbox_id, attempt, provider_id, status, accepted_at, delivered_at, error_code)`
- `verifier_verdict(incident_id, model_digest, input_hashes, verdict, score, calibration_version, latency_ms, shadow)`
- `feature_flag(scope_type/id, feature, mode, config_version, changed_by, changed_at)`
- `store_schedule_version` and `schedule_override` for holidays/temporary closures.

Do not retain per-frame detections. Emit track start/end, zone enter/exit and sparse dwell milestones. A planning bound of 20 tracks/camera/hour × 12 hours × 101 cameras × 4 transitions is about 97,000 transitions/day. At 1 KB/row+index this is roughly 100 MB/day and 1.4 GB/14 days; Phase 0 must replace this assumption with measured cardinality. Retain transitions 14 days, aggregate metrics 90 days, incident/audit metadata per policy, and video only for the approved evidence window. Verify deletion by sampled object and DB checks.

### Feature flags and kill switches

Modes: `off`, `shadow`, `enrich`, `downgrade`, `suppress`. Scope hierarchy: global → store → camera → camera/detector. More restrictive wins. Global switches: `DECISION_ADAPTER_ENABLED`, `OUTBOX_ENABLED`, `VERIFIER_ENABLED`, `VERIFIER_AUTHORITY_ENABLED`, `DELIVERY_ENABLED`. Defaults are off. `VERIFIER_AUTHORITY_ENABLED` requires a validated camera/detector allowlist and automatically drops to enrichment on timeout, calibration expiry, queue depth or drift breach.

### Phases, owners, SLAs and gates

| Phase | Goal and owner | Entry/acceptance | Safety risk and rollback |
|---|---|---|---|
| 0A Measurement | ML Quality; daily evidence review | 14-day protocol; immutable links; clocks; camera count; capacity; CIs | Bad labels create fake success. Roll back UI only; preserve evidence. |
| 0B Governance/reliability | Platform + Privacy; P0 response 15 min | RBAC/audit, outbox telemetry, Odoo replay test, RTO/RPO drill, verified deletion | Lockout/data loss. Additive schema; previous image must read it. |
| 1 Deterministic pain | Platform + CCTV; P1 one business day | ≥99% clip-eligible evidence; dedup; delivery SLO; no recall-margin breach | Tight rules suppress real events. Flag off + prior digest. |
| 2 Verifier shadow | ML Quality; weekly calibration | UCB false suppression below agreed ceiling; critical acceptance set zero; p95/p99 budget | False suppression/host overload. No authority; remote kill switch. |
| 3 Rules/scenarios | Loss Prevention + Platform | dry-run, approval, version, audit, rollback per rule | Non-engineer rule disables coverage. Approval and simulation mandatory. |
| 4 Search | Platform + Privacy | RBAC/store isolation, timezone and retention correctness | Cross-store footage leakage. Disable search index/API. |
| 5 Analytics/UX | Retail Ops + CCTV | calibrated entrance lines and per-camera tests | Misleading decisions from broken tracks. Label uncalibrated metrics. |

Phase 1 development can overlap 0B, but fleet authority waits for the relevant governance gate. Move camera-health, schedule migration, canonical evidence and outbox ahead of any VLM. Keep perception-dependent fire/weapon/fall work separate from the rules-engine phase.

### Canary and rollback

Representative candidates: Junction Ch1/Ch5 (mannequin and zone ambiguity), Digo (high-FPS/backlight), Sarit (0.5 FPS stress case), and Acacia (high latency/partial outage). A camera is eligible only after stream, clock, zone and evidence checks pass.

1. Pin every image by registry digest; retain current and previous known-good digests.
2. Back up Postgres and evidence metadata; restore into an isolated Compose project and validate counts/hashes.
3. Deploy additive nullable schema; run previous image against it in staging.
4. Run flags off, then shadow, one canary at a time. Compare exact baseline contracts.
5. Promote only after held-out acceptance. Freeze tuning before evaluation.
6. Roll back application digest and flags independently. Do not destructively downgrade schema; run a separate tested restore drill.

### Outage recovery

Proposed targets pending business approval: metadata RPO ≤5 minutes, evidence RPO ≤15 minutes, service RTO ≤30 minutes. Use an off-host encrypted object store in an approved jurisdiction; location and processor terms require a cross-border assessment. Restore-test monthly and after material schema changes. During total VivoGuard outage, NVR recording must continue independently—this is not yet verified per store—and the control room must use its existing telephone/security escalation tree. Platform Engineering declares technical recovery; Loss Prevention declares alerting operational after a live controlled test.

### Contract tests

1. Flags off and verifier timeout produce byte-semantic equivalent identifiers/type/severity/attribution/evidence/destination/review state.
2. Adapter p99 overhead ≤100 ms under measured burst; bounded queue sheds verifier work, never baseline alerts.
3. Transaction rollback creates neither orphan alert nor outbox row; retry creates one delivery by idempotency key.
4. Provider accepted/delivered/failed callbacks correlate to one attempt and tolerate duplicates/out-of-order callbacks.
5. Late verifier verdict after acknowledgement/resolution appends a transition and correction when needed.
6. Duplicate source alerts share one incident without losing members.
7. Previous application digest starts and processes alerts against the new schema.
8. Verifier OOM, network partition, cold start, corrupt output and queue flood leave inference FPS and baseline delivery within budget.
9. Evidence hashes, camera/time IDs and playable clip windows match staged events.
10. Retention deletes eligible objects and leaves immutable audit proof without retaining the deleted content.

## Explicit unknowns/blockers

- Unique physical-camera inventory versus endpoint rows.
- Natural-incident recall and detection-delay distribution.
- Actual notification destination and provider SLA; automated WhatsApp is disabled.
- NVR-independent recording continuity at every store.
- Off-host backup jurisdiction, restore history and incident response tree.
- Model licence/security review for any chosen verifier.
- Odoo credentials and authoritative store mapping.
- Authoritative store/weekend/holiday schedules.
- Competitor accuracy, latency, uptime and pricing without controlled trials and current quotations.

No live deployment, migration, alert change or production write was performed for this review.
