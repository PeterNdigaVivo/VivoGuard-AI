"""Retail alert policy: what a detection is allowed to become.

Every detection that could reach an operator passes through ``decide``
exactly once, and the answer is one of four outcomes:

    incident     actionable security or operational issue
    review       kept for a human to look at; never pages anyone
    metric_only  analytics only; no operator-facing row is needed
    discarded    low-confidence or invalid evidence

The function is deliberately pure: it reads only the ``PolicyInput`` it
is handed, so the same input always produces the same decision and the
decision can be unit-tested without Redis, a database or a camera. The
callers (the inference worker and the beat-driven info-alert path) are
responsible for gathering the context; this module is responsible for
the rule.

The decision and its reason are stamped on the DetectionEvent
(``extra.policy``) and on the Incident, so "why did this page me?" and
"why did this NOT page me?" both have a recorded answer.

Rules are kept here, in one place, on purpose. Detector code still does
its own pixel-level work (zone polygons, tracking, staff identity); the
policy decides what that evidence is worth to the business.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

POLICY_VERSION = "2026-11-26.1"

INCIDENT = "incident"
REVIEW = "review"
METRIC_ONLY = "metric_only"
DISCARDED = "discarded"
DECISIONS = (INCIDENT, REVIEW, METRIC_ONLY, DISCARDED)

# Operator-facing priority of an incident / review item.
URGENT = "urgent"
ATTENTION = "attention"
LOW = "low"

# Detectors that are counters, not judgements. Mirrors the worker's
# _SKIP_ALERT_TYPES plus the beat-produced informational types.
METRIC_TYPES: frozenset[str] = frozenset({
    "heatmap", "customer_journey", "unique_visitor", "entry_exit",
    "demographic", "occupancy_metrics", "occupancy", "passersby",
    "window_engagement", "dwell", "checkout_dwell_complete",
})

# Informational feeds that keep their legacy Alert row for the store
# dashboard but are never incidents.
INFORMATIONAL_TYPES: frozenset[str] = frozenset({
    "store_intelligence", "sales_floor_insight", "positive_operational",
})

# Camera and pipeline health is coverage, not security.
COVERAGE_TYPES: frozenset[str] = frozenset({"system_health", "camera_offline"})

# Kept as urgent incidents, but only with evidence.
SAFETY_CRITICAL_TYPES: frozenset[str] = frozenset({
    "fire", "smoke", "weapon", "weapon_brandished", "fight", "fall",
})

# Person-presence detectors whose meaning depends on the store schedule.
PRESENCE_TYPES: frozenset[str] = frozenset({"intrusion", "person", "trespass"})

# Generic or vague detectors: review-only until validated. "Validated"
# means the detector was explicitly listed in POLICY_VALIDATED_DETECTORS
# after its review precision met the bar.
DEFAULT_REVIEW_ONLY_TYPES: frozenset[str] = frozenset({
    "scene_review", "phone_usage",          # VLM scene review
    "shelf", "shelf_change",                # merchandise disarray
    "audio_distress",                       # raised voices
    "shrinkage", "live_activity",           # vague "security concern"
})

# Zone tags that count as a protected area or an entry for the
# before-hours rule.
PROTECTED_ZONE_TAGS: frozenset[str] = frozenset({
    "restricted", "intrusion", "trespass", "stockroom", "high_value",
    "staff_zone", "staff_only", "entry_exit", "entrance", "door",
})

STAFF_LEVELS_KNOWN: frozenset[str] = frozenset({"high", "medium"})


@dataclass(frozen=True)
class PolicyInput:
    detection_type: str
    confidence: float = 1.0
    extra: Mapping = field(default_factory=dict)
    zone_id: int | None = None
    zone_tags: frozenset[str] = frozenset()
    # Consecutive frames the underlying track has been seen. None means the
    # detector is not track-based or has its own frame gate.
    frames_seen: int | None = None
    min_frames: int = 4
    # The store has an explicit opening schedule (not the default window).
    has_schedule: bool = True
    # The caller already decided not to alert (normal trading traffic,
    # temporal gate, detector-specific suppression). Kept as a reason.
    suppressed_reason: str | None = None
    # Raw-review mode keeps a suppressed detection visible as review.
    raw_review: bool = False
    validated_types: frozenset[str] = frozenset()
    review_only_types: frozenset[str] = DEFAULT_REVIEW_ONLY_TYPES
    min_confidence: float = 0.25
    safety_min_confidence: float = 0.5


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    reason: str
    priority: str = ATTENTION

    @property
    def creates_alert_row(self) -> bool:
        return self.decision in (INCIDENT, REVIEW)

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason,
                "priority": self.priority, "version": POLICY_VERSION}


def _rule(inp: PolicyInput) -> str:
    ex = inp.extra or {}
    return str(ex.get("rule") or ex.get("cls") or "")


def _declared_priority(inp: PolicyInput) -> str:
    p = str((inp.extra or {}).get("priority") or "").lower()
    if p in ("critical", "high", "urgent"):
        return URGENT
    if p in ("info", "low", "positive"):
        return LOW
    return ATTENTION


def _temporally_confirmed(inp: PolicyInput) -> bool:
    if inp.frames_seen is None:
        return True
    return inp.frames_seen >= max(1, inp.min_frames)


def _is_static(inp: PolicyInput) -> bool:
    ex = inp.extra or {}
    return bool(ex.get("static") or ex.get("static_object")
                or ex.get("is_static"))


def _presence(inp: PolicyInput) -> PolicyDecision:
    ex = inp.extra or {}
    time_ctx = str(ex.get("time_context") or "")
    staff = str(ex.get("staff_level") or ex.get("identity_level") or "")
    if _is_static(inp):
        return PolicyDecision(DISCARDED, "static_object_not_person", LOW)
    if not _temporally_confirmed(inp):
        return PolicyDecision(DISCARDED, "not_confirmed_across_frames", LOW)

    if time_ctx == "before_hours":
        if staff in STAFF_LEVELS_KNOWN:
            return PolicyDecision(REVIEW, "expected_early_staff", LOW)
        if not inp.has_schedule:
            return PolicyDecision(REVIEW, "before_hours_no_store_schedule", LOW)
        if inp.zone_id is None or not (inp.zone_tags & PROTECTED_ZONE_TAGS):
            return PolicyDecision(
                REVIEW, "before_hours_no_protected_zone", LOW)
        return PolicyDecision(INCIDENT, "confirmed_before_hours_intrusion",
                              URGENT)

    after_hours = time_ctx == "after_hours" or bool(ex.get("after_hours"))
    if after_hours:
        if staff in STAFF_LEVELS_KNOWN:
            return PolicyDecision(REVIEW, "known_staff_after_hours", ATTENTION)
        return PolicyDecision(INCIDENT, "confirmed_after_hours_intrusion",
                              URGENT)

    # Trading hours: a presence alert only reaches here when a restricted
    # zone made it notable.
    if inp.zone_id is not None and inp.zone_tags & PROTECTED_ZONE_TAGS:
        return PolicyDecision(INCIDENT, "person_in_protected_zone",
                              _declared_priority(inp))
    return PolicyDecision(METRIC_ONLY, "normal_trading_presence", LOW)


def _staff_zone(inp: PolicyInput) -> PolicyDecision:
    ex = inp.extra or {}
    if inp.zone_id is None:
        return PolicyDecision(DISCARDED, "no_configured_zone", LOW)
    if _is_static(inp):
        return PolicyDecision(DISCARDED, "static_object_not_person", LOW)
    if not _temporally_confirmed(inp):
        return PolicyDecision(DISCARDED, "not_confirmed_across_frames", LOW)
    if ex.get("in_zone") is False:
        return PolicyDecision(DISCARDED, "person_outside_zone", LOW)
    if str(ex.get("identity_level") or "") in STAFF_LEVELS_KNOWN:
        return PolicyDecision(REVIEW, "likely_staff_in_staff_zone", LOW)
    return PolicyDecision(INCIDENT, "confirmed_person_in_staff_zone",
                          _declared_priority(inp))


def _store_opening(inp: PolicyInput) -> PolicyDecision:
    rule = _rule(inp)
    if rule == "shop_not_opened":
        if not inp.has_schedule:
            return PolicyDecision(REVIEW, "not_opened_without_schedule", LOW)
        return PolicyDecision(INCIDENT, "store_not_opened_by_schedule",
                              ATTENTION)
    # Opened / closed confirmations are operational facts, not incidents.
    return PolicyDecision(METRIC_ONLY, f"store_state:{rule or 'update'}", LOW)


def decide(inp: PolicyInput) -> PolicyDecision:
    """Return the single policy decision for one detection."""
    dtype = inp.detection_type

    if dtype in COVERAGE_TYPES:
        return PolicyDecision(METRIC_ONLY, "coverage_issue", LOW)
    if dtype in METRIC_TYPES:
        return PolicyDecision(METRIC_ONLY, "analytics_detector", LOW)
    if dtype in INFORMATIONAL_TYPES:
        return PolicyDecision(METRIC_ONLY, "informational_update", LOW)

    if inp.suppressed_reason:
        if inp.raw_review:
            return PolicyDecision(REVIEW, f"raw_review:{inp.suppressed_reason}",
                                  LOW)
        if inp.suppressed_reason in ("normal_trading_presence",
                                     "detector_suppressed"):
            return PolicyDecision(METRIC_ONLY, inp.suppressed_reason, LOW)
        return PolicyDecision(DISCARDED, inp.suppressed_reason, LOW)

    if inp.confidence < inp.min_confidence:
        return PolicyDecision(DISCARDED, "low_confidence", LOW)

    if dtype in SAFETY_CRITICAL_TYPES:
        if inp.confidence < inp.safety_min_confidence:
            return PolicyDecision(REVIEW, "safety_low_confidence", ATTENTION)
        if not _temporally_confirmed(inp):
            return PolicyDecision(REVIEW, "safety_not_confirmed_across_frames",
                                  ATTENTION)
        return PolicyDecision(INCIDENT, "safety_critical_with_evidence", URGENT)

    if dtype in inp.review_only_types and dtype not in inp.validated_types:
        return PolicyDecision(REVIEW, "review_only_until_validated", LOW)

    if dtype in PRESENCE_TYPES:
        return _presence(inp)
    if dtype == "staff_zone":
        return _staff_zone(inp)
    if dtype == "shop_open_close":
        return _store_opening(inp)

    if not _temporally_confirmed(inp):
        return PolicyDecision(DISCARDED, "not_confirmed_across_frames", LOW)
    return PolicyDecision(INCIDENT, "detector_rule_matched",
                          _declared_priority(inp))
