"""Plain-English zone-purpose mapping.

Operators in the new UI never see technical detection-type strings.
They pick a "purpose" from a dropdown; the API translates that to the
zone's `detection_types_json` list.

Add new entries here when you add a new detector that should be
operator-selectable.
"""
from __future__ import annotations

ZONE_PURPOSES = {
    "count_entries":     {
        "label": "Count people entering",
        "shape": "line",
        "types": ["entry_exit"],
        "description": "Two-point line. People crossing inward = +1, outward = −1.",
    },
    "count_entries_glass": {
        "label": "Monitor glass door",
        "shape": "line",
        # `glass_door` is the modifier tag the EntryExit detector reads
        # (alongside the standard `entry_exit`) to enable stricter
        # filtering for cameras pointed at reflective glass entrances.
        "types": ["entry_exit", "glass_door"],
        "description": (
            "Two-point line at a glass door. Same as 'Count people "
            "entering' but with stricter confidence and a 2-frame "
            "persistence requirement to ignore reflection flickers."
        ),
    },
    "checkout_queue":    {
        "label": "Monitor checkout queue",
        "shape": "polygon",
        "types": ["queue"],
        "description": "Polygon around the queueing area. Tracks length and wait.",
    },
    "staff_counter":     {
        "label": "Track staff at counter",
        "shape": "polygon",
        "types": ["counter"],
        "description": "Polygon around the till. Alerts when unstaffed > N seconds.",
    },
    "staff_zone":        {
        "label": "Staff Area (Behind Counter)",
        "shape": "polygon",
        "types": ["staff_zone"],
        "description": "Staff-only area behind the service counter. Alerts when a non-uniformed person enters.",
    },
    "product_aisle":     {
        "label": "Watch product aisle",
        "shape": "polygon",
        "types": ["aisle"],
        "description": "Polygon around the aisle. Reports avg customer dwell.",
    },
    "intrusion_zone":    {
        "label": "Detect after-hours intrusion",
        "shape": "polygon",
        "types": ["restricted"],
        "description": "Polygon for any restricted area. Alerts outside store hours.",
    },
    "stockroom":         {
        "label": "Monitor stockroom access",
        "shape": "polygon",
        "types": ["stockroom"],
        "description": "Polygon around the stockroom door. Logs every entry/exit.",
    },
    "shutter":           {
        "label": "Monitor shutter state",
        "shape": "polygon",
        "types": ["shutter"],
        "description": "Polygon over the shutter surface. Alerts on open-after-hours.",
    },
    "shelf":             {
        "label": "Monitor shelf for changes",
        "shape": "polygon",
        "types": ["shelf_change"],
        "description": "Polygon around a single shelf. Alerts when contents change.",
    },
    "high_value":        {
        "label": "Flag shrinkage near high-value display",
        "shape": "polygon",
        "types": ["high_value"],
        "description": "Polygon around an expensive display. Triggers loiter+no-staff alert.",
    },
    "sidewalk":          {
        "label": "Count sidewalk passersby",
        "shape": "polygon",
        "types": ["sidewalk"],
        "description": "Polygon over the sidewalk visible from the camera.",
    },
    "window_display":    {
        "label": "Measure window engagement",
        "shape": "polygon",
        "types": ["window"],
        "description": "Polygon at the window. Tracks stop rate / dwell time.",
    },
}


def types_for_purpose(purpose_key: str) -> list[str]:
    p = ZONE_PURPOSES.get(purpose_key)
    return list(p["types"]) if p else []
