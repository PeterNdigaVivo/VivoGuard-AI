"""Metric-gated model promotion.

A candidate AIModel only replaces production when, on the last K
days of operator-marked alerts:

  candidate.precision  >= production.precision    (no accuracy regression)
  candidate.fp_rate    <= production.fp_rate      (false-positive count drops)
  candidate.alerts_total >= MIN_PROMOTION_VOLUME  (enough data to trust)

When the candidate has never been deployed (and so has no
model_metrics rows), the rule short-circuits to "pending shadow"
status — operators can still force-deploy via the existing
/training/models/{id}/deploy endpoint, but the automated weekly
promotion task will skip it until enough traffic accumulates.

Rollback is the same `/deploy` endpoint pointed at the previous
version. AIModel.version is auto-incremented vN so the prior
version is always one row away.
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import AIModel, ModelMetric

log = logging.getLogger(__name__)

PROMOTION_WINDOW_DAYS  = 7
MIN_PROMOTION_VOLUME   = 20      # alerts the candidate must have served
PROMOTION_MIN_DELTA    = 0.0     # strict no-regression; tighten as
                                  # confidence grows


def _aggregate_metrics(db: Session, model_id: int,
                       *, since: date, detection_type: Optional[str] = None
                       ) -> dict:
    q = (db.query(func.sum(ModelMetric.tp_count),
                   func.sum(ModelMetric.fp_count),
                   func.sum(ModelMetric.alerts_total))
            .filter(ModelMetric.model_id == model_id,
                    ModelMetric.day >= since))
    if detection_type is not None:
        q = q.filter(ModelMetric.detection_type == detection_type)
    tp, fp, total = q.one()
    tp, fp, total = int(tp or 0), int(fp or 0), int(total or 0)
    triaged = tp + fp
    return {
        "model_id":     model_id,
        "tp_count":     tp,
        "fp_count":     fp,
        "alerts_total": total,
        "precision":    (tp / triaged) if triaged else None,
        "fp_rate":      (fp / total)   if total   else None,
    }


def evaluate_candidate(db: Session, candidate_id: int,
                       *, window_days: int = PROMOTION_WINDOW_DAYS,
                       min_volume: int = MIN_PROMOTION_VOLUME,
                       min_delta: float = PROMOTION_MIN_DELTA) -> dict:
    """Compare a candidate AIModel against the currently-deployed
    model with the same `name`. Returns a verdict dict — does NOT
    mutate anything."""
    cand = db.get(AIModel, candidate_id)
    if cand is None:
        return {"verdict": "error", "reason": "candidate not found"}

    prod = (db.query(AIModel)
              .filter(AIModel.name == cand.name,
                      AIModel.deployed == True,                       # noqa: E712
                      AIModel.id != cand.id)
              .order_by(AIModel.created_at.desc())
              .first())
    if prod is None:
        return {"verdict": "no_baseline",
                "reason": "no deployed sibling to compare against",
                "candidate_id": cand.id}

    since = date.today() - timedelta(days=window_days)
    cand_m = _aggregate_metrics(db, cand.id, since=since)
    prod_m = _aggregate_metrics(db, prod.id, since=since)

    if cand_m["alerts_total"] < min_volume:
        return {"verdict": "insufficient_volume",
                "reason": (f"candidate has {cand_m['alerts_total']} alerts "
                            f"in last {window_days}d (need >= {min_volume})"),
                "candidate": cand_m, "production": prod_m}

    # Edge case — production also has no triaged alerts: anything is
    # better than nothing, but be conservative and require triaged
    # candidate stats.
    if cand_m["precision"] is None:
        return {"verdict": "insufficient_volume",
                "reason": "candidate has no triaged alerts yet",
                "candidate": cand_m, "production": prod_m}

    prod_prec = prod_m["precision"] or 0.0
    prod_fpr  = prod_m["fp_rate"]   or 0.0
    cand_prec = cand_m["precision"] or 0.0
    cand_fpr  = cand_m["fp_rate"]   or 0.0

    precision_ok = (cand_prec - prod_prec) >= -min_delta
    fp_rate_ok   = (prod_fpr  - cand_fpr)  >= -min_delta

    return {
        "verdict": "promote" if (precision_ok and fp_rate_ok) else "reject",
        "candidate": cand_m,
        "production": prod_m,
        "precision_delta": cand_prec - prod_prec,
        "fp_rate_delta":   cand_fpr  - prod_fpr,
        "window_days": window_days,
    }


def promote(db: Session, candidate_id: int, *,
            force: bool = False, **eval_kwargs) -> dict:
    """Evaluate + (optionally) flip the candidate to deployed. Returns
    the evaluation result; on a "promote" verdict (or force=True),
    also sets candidate.deployed=True and demotes any sibling
    deployed model with the same name."""
    result = evaluate_candidate(db, candidate_id, **eval_kwargs)
    if not force and result.get("verdict") != "promote":
        return result

    cand = db.get(AIModel, candidate_id)
    siblings = (db.query(AIModel)
                  .filter(AIModel.name == cand.name,
                          AIModel.deployed == True,                   # noqa: E712
                          AIModel.id != cand.id)
                  .all())

    # Incremental-regression auto-revert (Part 2 #7, Q2): never deploy a
    # candidate whose training map50 is BELOW the currently-deployed model.
    # `force` (explicit operator override) bypasses this guard.
    if not force and cand is not None and cand.map50 is not None:
        deployed_best = max((s.map50 for s in siblings if s.map50 is not None),
                            default=None)
        if deployed_best is not None and cand.map50 < deployed_best:
            log.warning("incremental regression — keeping current deployed model "
                        "(candidate=%s map50=%.4f < deployed map50=%.4f)",
                        candidate_id, cand.map50, deployed_best)
            result["verdict"] = "regression"
            result["promoted"] = False
            result["reason"] = (f"candidate map50 {cand.map50:.4f} < deployed "
                                f"{deployed_best:.4f} — not promoted")
            return result
    for s in siblings:
        s.deployed = False
    cand.deployed = True
    db.commit()
    log.info("promotion: candidate=%s name=%s deployed (demoted %d sibling(s)) "
             "verdict=%s force=%s",
             candidate_id, cand.name, len(siblings),
             result.get("verdict"), force)
    result["promoted"] = True
    result["demoted_ids"] = [s.id for s in siblings]
    return result
