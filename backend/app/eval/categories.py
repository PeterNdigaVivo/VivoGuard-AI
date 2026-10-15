"""Which alerts the evaluation test set covers.

Kept dependency-free: inference_worker imports it on the alert path.
"""
from __future__ import annotations

COUNTER_UNATTENDED = "counter_unattended"   # staff_present / rule counter_unattended
PERSON = "person"                           # person, intrusion, trespass
UNIFORM = "uniform"                         # uniform_compliance

CATEGORIES = (COUNTER_UNATTENDED, PERSON, UNIFORM)

_PERSON_TYPES = frozenset({"person", "intrusion", "trespass"})
# Every detection_type that can map to a category (for database filters).
EVALUATED_TYPES = frozenset(_PERSON_TYPES | {"staff_present", "uniform_compliance"})


def eval_category(detection_type: str | None, rule: str | None = None) -> str | None:
    """The test-set category for an alert, or None when it is not evaluated."""
    if detection_type == "staff_present" and rule == "counter_unattended":
        return COUNTER_UNATTENDED
    if detection_type in _PERSON_TYPES:
        return PERSON
    if detection_type == "uniform_compliance":
        return UNIFORM
    return None
