"""Tests for app.utils.zone_health.score_store — Feature 4 rubric.

Pure scoring; no DB. Every scenario from Stephen's audit list (and
the rubric's edge cases) has a named test below so a regression is
easy to point at.
"""
from __future__ import annotations
from app.utils.zone_health import (
    BADGE_HEALTHY_MIN, BADGE_WARN_MIN, POINTS_PER_REQUIRED,
    PENALTY_DUPLICATE, PENALTY_TYPO, PENALTY_OFFLINE_CAM,
    score_store, _detect_typo,
)


# ---- helpers ---------------------------------------------------------

def _cam(cid: int, name: str | None = None, status: str = "online") -> dict:
    return {"id": cid, "name": name or f"Cam {cid}", "status": status}


def _zone(zid: int, cam_id: int, name: str, *tags: str) -> dict:
    return {"id": zid, "camera_id": cam_id, "name": name,
            "detection_types_json": list(tags)}


# Standard 4-camera, all-zones-covered store. The "Runda template".
def _healthy_store_fixture():
    cams = [_cam(1), _cam(2), _cam(3), _cam(4)]
    zones = [
        _zone(10, 1, "Front door footfall", "entry_exit"),
        _zone(11, 2, "Checkout queue",      "queue"),
        _zone(12, 3, "Staff area",          "staff_zone"),
        _zone(13, 4, "Shutter sensor",      "shutter"),
    ]
    return cams, zones


# ---- score & badge bands --------------------------------------------

def test_all_four_zones_present_scores_100_healthy():
    cams, zones = _healthy_store_fixture()
    r = score_store(1, "Runda", cams, zones)
    assert r["score"] == 100
    assert r["badge"] == "healthy"
    assert all(r["per_tag_present"].values())
    assert r["issues"] == []


def test_missing_shutter_loses_25():
    cams, zones = _healthy_store_fixture()
    # Drop the shutter zone (zid=13).
    zones = [z for z in zones if z["id"] != 13]
    r = score_store(2, "Garden City", cams, zones)
    assert r["score"] == 75
    assert r["badge"] == "warn"
    assert r["per_tag_present"]["shutter"] is False
    assert any(i["kind"] == "missing_tag" and i["tag"] == "shutter"
               for i in r["issues"])


def test_missing_all_four_zones_scores_zero_critical():
    cams = [_cam(1), _cam(2), _cam(3), _cam(4)]
    r = score_store(3, "Bare store", cams, [])
    assert r["score"] == 0
    assert r["badge"] == "critical"
    assert not any(r["per_tag_present"].values())


# ---- duplicate rule — SAME-camera only, never across cameras --------

def test_two_cameras_each_with_entry_exit_is_NOT_duplicate():
    """The Runda case: cam 59 has footfall + cam 62 has footfall =
    intentional coverage, not a penalty. Spec's most important rule."""
    cams = [_cam(59), _cam(60), _cam(61), _cam(62)]
    zones = [
        _zone(100, 59, "Front door", "entry_exit"),
        _zone(101, 62, "Side door",  "entry_exit"),
        _zone(102, 60, "Checkout",   "queue"),
        _zone(103, 61, "Staff area", "staff_zone"),
        _zone(104, 60, "Shutter",    "shutter"),
    ]
    r = score_store(4, "Runda", cams, zones)
    assert r["score"] == 100
    assert not any(i["kind"] == "duplicate_zone" for i in r["issues"])


def test_camera_14_two_entry_exit_zones_is_duplicate_minus_10():
    """The audit's actual Yaya bug: cam 14 carries TWO entry_exit
    zones on the same camera → double-counting → −10."""
    cams = [_cam(13), _cam(14), _cam(15), _cam(16)]
    zones = [
        _zone(200, 14, "Main door",       "entry_exit"),
        _zone(201, 14, "Main door (dup)", "entry_exit"),  # the bug
        _zone(202, 15, "Checkout",        "queue"),
        _zone(203, 16, "Staff area",      "staff_zone"),
        _zone(204, 13, "Shutter",         "shutter"),
    ]
    r = score_store(5, "Yaya", cams, zones)
    assert r["score"] == 100 - PENALTY_DUPLICATE
    dup_issues = [i for i in r["issues"] if i["kind"] == "duplicate_zone"]
    assert len(dup_issues) == 1
    assert dup_issues[0]["camera_id"] == 14
    assert dup_issues[0]["count"] == 2
    assert dup_issues[0]["tag"] == "entry_exit"


def test_camera_152_triple_entry_exit_zones_is_two_extras_minus_20():
    """Safari Sarit bug: 3 entry_exit zones on cam 152 = 2 extras."""
    cams = [_cam(150), _cam(151), _cam(152), _cam(154)]
    zones = [
        _zone(300, 152, "Door A", "entry_exit"),
        _zone(301, 152, "Door B", "entry_exit"),
        _zone(302, 152, "Door C", "entry_exit"),
        _zone(303, 150, "Checkout",    "queue"),
        _zone(304, 151, "Staff area",  "staff_zone"),
        _zone(305, 154, "Shutter",     "shutter"),
    ]
    r = score_store(6, "Safari Sarit", cams, zones)
    assert r["score"] == 100 - 2 * PENALTY_DUPLICATE
    dup = [i for i in r["issues"] if i["kind"] == "duplicate_zone"][0]
    assert dup["count"] == 3


# ---- typo detection -------------------------------------------------

def test_typo_checkuout_flags_with_suggestion():
    """Audit's Moi Avenue cam 38 typo. The suggested rename preserves
    the rest of the zone name and replaces only the typo word — so
    operators see exactly the before/after Stephen asked for."""
    cams, zones = _healthy_store_fixture()
    # Rename the existing checkout queue zone to the audit's typo.
    for z in zones:
        if z["id"] == 11:
            z["name"] = "Checkuout"
    r = score_store(7, "Moi Avenue", cams, zones)
    assert r["score"] == 100 - PENALTY_TYPO
    typo = [i for i in r["issues"] if i["kind"] == "typo_name"][0]
    assert typo["current_name"]   == "Checkuout"
    assert typo["suggested_name"] == "Checkout"
    assert typo["similarity"] >= 0.6


def test_typo_conter_and_shinkage_both_flag():
    """Audit's Galleria cam 75 ('conter') + cam 77 ('Shinkage').
    Both typo zones use non-required tags (aisle / intrusion) so the
    test isolates the typo penalty from any duplicate penalty."""
    cams, zones = _healthy_store_fixture()
    zones.extend([
        _zone(20, 1, "conter",   "aisle"),
        _zone(21, 2, "Shinkage", "intrusion"),
    ])
    r = score_store(8, "Galleria", cams, zones)
    typos = [i for i in r["issues"] if i["kind"] == "typo_name"]
    names = {t["current_name"] for t in typos}
    assert "conter"   in names
    assert "Shinkage" in names
    # Verify the rename preview Stephen explicitly asked for.
    by_name = {t["current_name"]: t for t in typos}
    assert by_name["conter"]["suggested_name"]   == "counter"
    assert by_name["Shinkage"]["suggested_name"] == "Shrinkage"
    assert r["score"] == 100 - 2 * PENALTY_TYPO


def test_canonical_keywords_not_flagged():
    """The TYPO_KEYWORDS list (the words operators correctly spell)
    must never be flagged as typos. Also: correctly-spelled multi-
    word zone names made from keywords + stopwords must pass through
    cleanly — operators don't get noise on legitimate shortenings."""
    from app.utils.zone_health import TYPO_KEYWORDS
    for kw in TYPO_KEYWORDS:
        is_typo, _, _ = _detect_typo(kw)
        assert not is_typo, f"keyword {kw!r} wrongly flagged"
    for name in (
        "Checkout queue", "Front door footfall", "Staff area",
        "Shutter sensor", "Aisle 3", "Stockroom door",
        "Counter behind till",
    ):
        is_typo, _, _ = _detect_typo(name)
        assert not is_typo, f"clean name {name!r} wrongly flagged"


# ---- offline cameras -------------------------------------------------

def test_one_offline_camera_minus_15():
    cams, zones = _healthy_store_fixture()
    cams[0]["status"] = "offline"
    r = score_store(9, "Mama Ngina", cams, zones)
    assert r["score"] == 100 - PENALTY_OFFLINE_CAM
    assert r["cameras_offline"] == 1
    assert r["cameras_online"]  == 3
    assert any(i["kind"] == "camera_offline" for i in r["issues"])


def test_all_cameras_offline_score_clamps_to_zero():
    """Audit's Kileleshwa case — 4×−15=−60 with already-zero base
    (no zones either) wouldn't underflow."""
    cams = [_cam(i, status="offline") for i in (1, 2, 3, 4)]
    r = score_store(10, "Kileleshwa", cams, [])   # also no zones
    assert r["score"] == 0
    assert r["badge"]  == "critical"
    assert r["cameras_offline"] == 4


def test_pending_camera_status_neither_online_nor_offline():
    """A pending camera (new, awaiting first frame) is not counted
    as offline → no −15 penalty."""
    cams, zones = _healthy_store_fixture()
    cams[0]["status"] = "pending"
    r = score_store(11, "New store", cams, zones)
    assert r["score"] == 100   # no penalty
    assert r["cameras_offline"] == 0
    assert r["cameras_online"]  == 3


# ---- empty-store edge case ------------------------------------------

def test_store_with_zero_cameras_returns_no_cameras_critical():
    r = score_store(12, "Brand-new store", [], [])
    assert r["score"] == 0
    assert r["badge"] == "critical"
    assert r["cameras_total"] == 0
    assert r["issues"] == [{"kind": "no_cameras",
                             "message": "No cameras configured"}]
    # Per-tag-presence map still well-formed even for empty input.
    for v in r["per_tag_present"].values():
        assert v is False


# ---- badge thresholds -----------------------------------------------

def test_badge_band_boundaries():
    assert BADGE_HEALTHY_MIN == 90
    assert BADGE_WARN_MIN    == 60
    # Exactly 90 → healthy; 89 → warn; 60 → warn; 59 → critical.
    def b(s): return score_store(99, "x",
                                  [_cam(1)],
                                  []).__class__   # only to silence linter
    from app.utils.zone_health import _badge
    assert _badge(100) == "healthy"
    assert _badge(90)  == "healthy"
    assert _badge(89)  == "warn"
    assert _badge(60)  == "warn"
    assert _badge(59)  == "critical"
    assert _badge(0)   == "critical"


def test_constants_match_spec():
    assert POINTS_PER_REQUIRED == 25
    assert PENALTY_DUPLICATE   == 10
    assert PENALTY_TYPO        == 5
    assert PENALTY_OFFLINE_CAM == 15
