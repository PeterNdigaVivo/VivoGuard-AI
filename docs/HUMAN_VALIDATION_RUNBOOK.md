# Human validation hand-off

## Gating dependency

The remaining dependency for an evidence-backed accuracy claim is independent
human ground truth. It is intentionally isolated from inference, alert delivery,
camera health and software deployment: those services continue while validation
work is pending.

**Accountable owner:** the Loss Prevention / CCTV operations validation lead.

**People required:** three named people using separate VivoOps accounts. One
performs primary review, another blind independent review, and a third adjudicates
disagreements. No person may perform two roles on the same incident.

## Exact action

1. Open VivoOps and go to **AI Learning → Labelling Sprint**.
2. Primary reviewer classifies the assigned, evidence-backed batch.
3. Independent reviewer signs in separately, selects **Independent review**, and
   completes the same evidence without being shown the first verdict.
4. Third reviewer selects **Resolve disagreements**, opens the incident evidence,
   records a clear rationale and selects true alert, false alert or unclear.
5. Select **Measure recall**, enter the agreed campaign seed and generate the
   random-footage batch. Both reviewers watch each clip without first checking
   whether VivoGuard alerted. A third reviewer resolves disagreement.
6. Operators use **Report missed alert** for independently found real incidents
   that had no correct alert. Do not enter names, biometrics or accusations.
7. Run separate seeded campaigns covering day/night and quiet/busy periods.

## Verification

The software checkpoint is successful when:

- the independent queue decreases under a second account;
- disagreement cases can be resolved only by a third account;
- agreed/adjudicated samples become eligible only when the camera-detector pair
  is active;
- unclear/disputed/quality-controlled evidence remains quarantined;
- scorecards show distinct sample size, precision confidence bounds, reviewer
  agreement and separately measured missed events/recall;
- governance audit records identify each action without exposing personal data.
- reviewed random clips are deleted after seven days while the non-video audit
  evidence and derived recall result remain available.

Do not state that 99% has been achieved until both precision and recall meet the
approved threshold on representative held-out production evidence. Simulation is
a regression check, not a substitute for this validation.

## Resume checkpoint

Repository branch: `codex/training-data-integrity`.

After the validation owner completes the first governed batch, instruct Codex:

> Resume VivoGuard from `docs/HUMAN_VALIDATION_RUNBOOK.md`. Verify the deployed
> commit and Alembic head, inspect the independent-review, disagreement and
> missed-event evidence, calculate camera/detector scorecards and confidence
> bounds, quarantine failing pairs, run the complete verification suite, and
> report whether the evidence gate passes without assuming 99%.
