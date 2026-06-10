"""Tests for the multi-evidence counter-attendance model.

The end-to-end Celery / Redis / detector wiring would need a full
test harness to drive (DB session, frame buffer, IOU tracker,
business hours…) and the project doesn't ship one yet. The
DECISION logic — per-person scoring + hysteresis — lives in pure
classmethods on StaffPresenceDetector, and those are what this
file drives directly.

Every scenario listed in the original ticket has a named test
below so a regression is easy to point at.
"""
from __future__ import annotations
from app.ai.detectors.retail_p2 import StaffPresenceDetector as D


# ---- score_uniform --------------------------------------------------

def test_uniform_top_ok_scores_40():
    assert D.score_uniform({"top_ok": True, "top_share": 0.45}) == 40


def test_uniform_partial_scores_20():
    """Borderline read — top_share ≥ 10 % but `top_ok` False
    (overhead camera + bad light). Worth half the uniform points."""
    assert D.score_uniform({"top_ok": False, "top_share": 0.12}) == 20


def test_uniform_below_partial_scores_0():
    assert D.score_uniform({"top_ok": False, "top_share": 0.04}) == 0


def test_uniform_none_scores_0():
    """Camera couldn't read the pixels at all (returns None from
    uniform_features). Must not assume anything."""
    assert D.score_uniform(None) == 0


# ---- score_identity -------------------------------------------------

def test_identity_high_30():    assert D.score_identity("high") == 30
def test_identity_medium_20():  assert D.score_identity("medium") == 20
def test_identity_uncertain_10(): assert D.score_identity("uncertain") == 10
def test_identity_unknown_0():  assert D.score_identity("unknown") == 0
def test_identity_none_0():     assert D.score_identity(None) == 0


# ---- score_dwell ----------------------------------------------------

def test_dwell_zero_scores_0():       assert D.score_dwell(0) == 0
def test_dwell_negative_scores_0():   assert D.score_dwell(-3.0) == 0


def test_dwell_15s_scores_1():
    """1 pt per 15 s. Floors via int()."""
    assert D.score_dwell(15.0) == 1


def test_dwell_150s_scores_10():
    assert D.score_dwell(150.0) == 10


def test_dwell_caps_at_20_after_5min():
    """Cap reached at 300 s (5 min) — anything longer still 20."""
    assert D.score_dwell(300.0) == 20
    assert D.score_dwell(3600.0) == 20      # 1 h still 20


# ---- person_score ---------------------------------------------------

def test_uniformed_cashier_seated_long_dwell_maxes_score():
    """Spec scenario: SEATED CASHIER in uniform, dwelled 10 min.
    40 (uniform) + 30 (high identity) + 20 (dwell cap) + 10 (zone
    floor) = 100."""
    feats = {"top_ok": True, "top_share": 0.5}
    assert D.person_score(feats, "high", 600) == 100


def test_standing_uniformed_staff_just_arrived():
    """Spec scenario: STANDING UNIFORMED STAFF — just walked into
    zone. Uniform high, identity high, dwell ~0. 40+30+0+10 = 80."""
    feats = {"top_ok": True, "top_share": 0.4}
    assert D.person_score(feats, "high", 0) == 80


def test_seated_cashier_with_failed_uniform_read():
    """Spec scenario: UNIFORM DETECTION FAILURE on a seated cashier
    (uniform_features returns None this frame). Identity stays
    medium via dwell-based staff_identity rule; full dwell credit;
    zone floor. 0 + 20 + 20 + 10 = 50 → stays attended via
    hysteresis."""
    assert D.person_score(None, "medium", 600) == 50


def test_customer_at_checkout_scores_low():
    """Spec scenario: CUSTOMER AT CHECKOUT (no uniform, no dwell,
    just standing at the till). 0 + 0 + 0 + 10 = 10. Far below the
    attended threshold — but the test is that this on its own
    doesn't BLOCK an unattended alert. Real-world the cashier is
    usually also in-zone so MAX(cashier=80, customer=10) wins."""
    feats = {"top_ok": False, "top_share": 0.05}
    assert D.person_score(feats, "unknown", 0) == 10


def test_temporary_occlusion_uniform_uncertain():
    """Spec scenario: TEMPORARY OCCLUSION — cashier briefly hidden
    behind a tall customer. uniform reads None, identity drops to
    uncertain, dwell still counts. 0 + 10 + 20 + 10 = 40 — exactly
    at the unattended threshold; hysteresis (40–60 keeps prior
    state) protects against flipping."""
    assert D.person_score(None, "uncertain", 600) == 40


def test_genuine_intruder_at_counter_scores_low():
    """No uniform, no staff identity, no dwell history. The +10
    zone floor is the only score. 10 ≪ 40 — passes through to the
    unattended-countdown path."""
    feats = {"top_ok": False, "top_share": 0.0}
    assert D.person_score(feats, "unknown", 0) == 10


# ---- apply_hysteresis ----------------------------------------------

def test_score_60_attended_regardless_of_prior():
    assert D.apply_hysteresis(60, previously_attended=False) is True
    assert D.apply_hysteresis(60, previously_attended=True)  is True
    assert D.apply_hysteresis(100, previously_attended=False) is True


def test_score_below_40_unattended_regardless_of_prior():
    assert D.apply_hysteresis(39, previously_attended=True)  is False
    assert D.apply_hysteresis(0,  previously_attended=True)  is False
    assert D.apply_hysteresis(10, previously_attended=False) is False


def test_score_in_40_60_band_keeps_prior_state():
    """Hysteresis band — prevents single-frame jitter from flipping
    a previously-attended counter (e.g. cashier momentarily looks
    away from the camera so identity briefly drops)."""
    assert D.apply_hysteresis(50, previously_attended=True)  is True
    assert D.apply_hysteresis(50, previously_attended=False) is False
    # Edge cases at the band boundaries.
    assert D.apply_hysteresis(59, previously_attended=True)  is True
    assert D.apply_hysteresis(59, previously_attended=False) is False
    assert D.apply_hysteresis(40, previously_attended=True)  is True
    assert D.apply_hysteresis(40, previously_attended=False) is False


# ---- threshold constants kept honest --------------------------------

def test_thresholds_match_spec():
    assert D.SCORE_ATTENDED          == 60
    assert D.SCORE_UNATTENDED        == 40
    assert D.UNATTENDED_DWELL_SECONDS == 5 * 60
    assert D.ABSENCE_GRACE_SECONDS    == 60
    assert D.DEDUP_SECONDS            == 30 * 60
