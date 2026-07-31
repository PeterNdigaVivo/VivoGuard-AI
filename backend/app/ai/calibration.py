"""Dynamic confidence calibration — per-zone and per-time-of-day.

Static per-camera thresholds fail in bad lighting: a glass-door camera
that's crisp at noon is glare-blown at 07:30, and a fixed 0.5 either
floods false positives in the morning or misses everything at midday.

Per-camera×detection-type base thresholds already exist
(`DetectionConfig.confidence_threshold`). This module layers two
OPTIONAL refinements, both read from `detection_configs.extra` so no
schema change is needed:

    extra = {
      "zone_conf_overrides": {"12": 0.65, "14": 0.4},   # zone_id → threshold
      "conf_time_bands": [                              # EAT local time
        {"start": "07:00", "end": "09:00", "multiplier": 0.85},
        {"start": "18:30", "end": "20:00", "multiplier": 0.9}
      ]
    }

Resolution order: zone override (absolute) replaces the base threshold,
then any matching time band's multiplier scales the result. The final
value is clamped to [0.05, 0.99]. Detectors opt in by calling
`effective_threshold(cfg, zone_id=...)` where they currently read
`cfg.get("confidence_threshold")` — wired into EntryExitDetector first
(glass-door glare is the worst offender); other detectors can adopt the
same one-line call as needed.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

EAT = ZoneInfo("Africa/Nairobi")

_CLAMP_LO: float = 0.05
_CLAMP_HI: float = 0.99


def _parse_hhmm(raw: object) -> time | None:
    try:
        hh, mm = str(raw).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def effective_threshold(cfg: dict | None, *,
                        zone_id: int | None = None,
                        default: float = 0.5,
                        now_eat: datetime | None = None) -> float:
    """Resolve the confidence threshold for a detector run.

    Args:
        cfg:      the per-camera detector config dict (as built by
                  _load_camera_state) — reads `confidence_threshold`
                  and the optional `extra` calibration mapping.
        zone_id:  when given, `extra.zone_conf_overrides[str(zone_id)]`
                  replaces the base threshold.
        default:  fallback when no threshold is configured at all.
        now_eat:  injection point for tests; defaults to now in EAT.

    Invalid/malformed entries are ignored (the base threshold applies) —
    a typo in a calibration mapping must degrade to current behaviour,
    never crash a detector.
    """
    cfg = cfg or {}
    extra = cfg.get("extra") or {}

    try:
        thr = float(cfg.get("confidence_threshold") or default)
    except (TypeError, ValueError):
        thr = default

    overrides = extra.get("zone_conf_overrides") or {}
    if zone_id is not None and isinstance(overrides, dict):
        raw = overrides.get(str(zone_id), overrides.get(zone_id))
        if raw is not None:
            try:
                thr = float(raw)
            except (TypeError, ValueError):
                pass

    bands = extra.get("conf_time_bands") or []
    if bands:
        now = (now_eat or datetime.now(EAT)).timetz().replace(tzinfo=None)
        for band in bands:
            if not isinstance(band, dict):
                continue
            start = _parse_hhmm(band.get("start"))
            end = _parse_hhmm(band.get("end"))
            if start is None or end is None or not (start <= now < end):
                continue
            try:
                thr *= float(band.get("multiplier", 1.0))
            except (TypeError, ValueError):
                continue
            break                       # first matching band wins

    return max(_CLAMP_LO, min(_CLAMP_HI, thr))
