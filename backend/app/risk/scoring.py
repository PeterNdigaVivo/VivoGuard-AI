"""Transparent review-priority scoring. This module has no side effects."""


def risk_band(score: float) -> str:
    if score >= 0.65:
        return "high_review"
    if score >= 0.35:
        return "medium_review"
    return "low_review"


def score_operational_event(event_type: str, amount: float | None, *,
                            after_hours: bool, camera_evidence: bool) -> tuple[float, list[dict]]:
    weights = {
        "refund": 0.25, "void": 0.30, "discount": 0.20,
        "no_sale": 0.35, "high_value_transaction": 0.20,
        "delivery_received": 0.10, "stock_move": 0.10, "stock_exit": 0.30,
    }
    factors = [{"signal": event_type, "weight": weights.get(event_type, 0.10)}]
    if amount is not None and amount >= 50_000:
        factors.append({"signal": "high_value_amount", "weight": 0.15})
    if after_hours:
        factors.append({"signal": "outside_business_hours", "weight": 0.25})
    if not camera_evidence:
        factors.append({"signal": "camera_evidence_unavailable", "weight": 0.15})
    return round(min(1.0, sum(float(f["weight"]) for f in factors)), 4), factors
